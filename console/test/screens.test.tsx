import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Grants } from '../src/api/grants';
import { createSession, STORAGE_KEY } from '../src/auth/session';
import { type Handler, json, problem } from './fakeApi';
import { chooseEnv, openSection, openTab, whoami } from './appPage';
import { signedIn, start, toast, WHOAMI } from './harness';

const OWNER = 'usr_cccccccccccccccccccc';
const OTHER = 'usr_dddddddddddddddddddd';
const APP = {
  id: 'app_aaaaaaaaaaaaaaaaaaaa',
  slug: 'expenses',
  owner_user_id: OWNER,
  owner_state: 'active',
  status: 'active',
  created_at: '2026-09-28T10:00:00Z',
  environments: [
    { id: 'env_preview0000000000000', name: 'preview', config_version: 1, form_targets: [], grants_version: 0, current_deployment_id: null },
    { id: 'env_prod0000000000000000', name: 'prod', config_version: 2, form_targets: [], grants_version: 3, current_deployment_id: 'dep_eeeeeeeeeeeeeeeeeeee' },
  ],
} as const;
const PROD_GRANTS = `/v1/apps/${APP.id}/environments/env_prod0000000000000000/grants`;
const PREVIEW_GRANTS = `/v1/apps/${APP.id}/environments/env_preview0000000000000/grants`;
const ORG_USER = { id: 'gnt_000000000000000000o1', role: 'user', subject_kind: 'org', subject_id: null, subject_name: null, subject_email: null } as const;
const OWNER_BUILDER = { id: 'gnt_000000000000000000b1', role: 'builder', subject_kind: 'user', subject_id: OWNER, subject_name: 'Olive Owner', subject_email: 'olive@example.com' } as const;
function prodGrants(version: number, list: Grants['grants']): Grants {
  return { environment_id: 'env_prod0000000000000000', grants_version: version, grants: list };
}

describe('login', () => {
  it('sends a visitor without a token to the login page and back after dev sign-in', async () => {
    const { api, router, session } = start('/apps/app_aaaaaaaaaaaaaaaaaaaa', {
      [`GET /v1/apps/${APP.id}`]: () => json(200, APP),
      [`GET ${PROD_GRANTS}`]: () => json(200, prodGrants(3, [])),
      [`GET ${PREVIEW_GRANTS}`]: () => json(200, { ...prodGrants(0, []), environment_id: 'env_preview0000000000000' }),
    });
    const box = await screen.findByLabelText('API token');
    expect(router.state.location.pathname).toBe('/login');
    fireEvent.change(box, { target: { value: '  tok-pasted \n' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('heading', { name: 'expenses' })).toBeTruthy();
    expect(router.state.location.pathname).toBe(`/apps/${APP.id}`);
    expect(session.token()).toBe('tok-pasted');
    expect(api.of('GET', '/v1/whoami')[0]?.headers.get('Authorization')).toBe('Bearer tok-pasted');
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('keeps the token for the tab only when asked', async () => {
    const session = createSession(window.sessionStorage);
    start('/login', { 'GET /v1/apps': () => json(200, { apps: [] }) }, session);
    fireEvent.change(await screen.findByLabelText('API token'), { target: { value: 'tok-kept' } });
    fireEvent.click(screen.getByLabelText('Keep it for this tab'));
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByRole('heading', { name: 'Apps' });
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe('tok-kept');
    expect(createSession(window.sessionStorage).token()).toBe('tok-kept');
  });

  it('refuses a token the API refuses and keeps nothing', async () => {
    const { session, router } = start('/login', {
      'GET /v1/whoami': () => problem(401, 'UNAUTHENTICATED', 'Authentication required'),
    });
    fireEvent.change(await screen.findByLabelText('API token'), { target: { value: 'bad' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Authentication required');
    expect(alert.textContent).toContain('UNAUTHENTICATED');
    expect(session.token()).toBeNull();
    expect(router.state.location.pathname).toBe('/login');
  });

  it('sends a signed-in person to the login page within 5 s of the API refusing them', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let refused = false;
      const { api, router, session } = start(
        '/',
        {
          'GET /v1/whoami': () => (refused ? problem(401, 'UNAUTHENTICATED', 'Authentication required') : WHOAMI()),
          'GET /v1/apps': () => json(200, { apps: [] }),
        },
        signedIn(),
      );
      await screen.findByRole('heading', { name: 'Apps' });
      const before = api.of('GET', '/v1/whoami').length;
      refused = true;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4999);
      });
      await waitFor(() => expect(router.state.location.pathname).toBe('/login'));
      expect(api.of('GET', '/v1/whoami').length).toBe(before + 1);
      expect(session.token()).toBeNull();
      expect(router.state.location.search).toEqual({ next: '/' });
    } finally {
      vi.useRealTimers();
    }
  });

  it('ignores a next path that leaves the site', async () => {
    const { router } = start('/login?next=%2F%2Fevil.example%2F', { 'GET /v1/apps': () => json(200, { apps: [] }) });
    fireEvent.change(await screen.findByLabelText('API token'), { target: { value: 'tok' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByRole('heading', { name: 'Apps' });
    expect(router.state.location.href).toBe('/');
  });
});

/** The status filter: one of a few, as radio buttons. */
function chooseStatus(name: 'All' | 'Active' | 'Disabled' | 'Quarantined') {
  fireEvent.click(within(screen.getByRole('group', { name: 'Status' })).getByRole('radio', { name }));
}

/** The slug of each app listed, in order: the text of the link that opens it. */
function listedSlugs(): (string | null)[] {
  return screen
    .getAllByRole('row')
    .slice(1)
    .map((r) => within(r).getByRole('link').textContent);
}

describe('your apps', () => {
  // Anyone but an org admin reads the apps they may open; an admin reads the inventory (below).
  const member = { 'GET /v1/whoami': whoami('member') };

  it('shows the refusal and its request id', async () => {
    start('/', { ...member, 'GET /v1/me/apps': () => problem(403, 'FORBIDDEN', 'Not allowed') }, signedIn());
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Not allowed');
    expect(alert.textContent).toContain('req_test_0001');
  });

  it('returns to the login page when the token stops working', async () => {
    const session = signedIn();
    const { router } = start('/', { ...member, 'GET /v1/me/apps': () => problem(401, 'UNAUTHENTICATED', 'Expired') }, session);
    await screen.findByLabelText('API token');
    expect(session.token()).toBeNull();
    expect(router.state.location.pathname).toBe('/login');
  });
});

describe('inventory for an org admin', () => {
  const PROD_ENV = 'env_prod0000000000000000';
  const PREVIEW_ENV = 'env_preview0000000000000';
  function item(slug: string, overrides: Record<string, unknown> = {}) {
    return {
      app_id: `app_${slug.padEnd(20, '0').slice(0, 20)}`,
      slug,
      status: 'active',
      created_at: '2026-09-28T10:00:00Z',
      owner_state: 'active',
      owner: { user_id: OWNER, display_name: 'Olivia Owner', name: 'Olivia Owner', email: 'olivia@example.com' },
      last_used_at: null,
      environments: [
        {
          environment_id: `env_${slug.padEnd(16, '0').slice(0, 16)}prod`,
          name: 'prod',
          current_release: null,
          last_deploy: null,
          sharing: { org_wide: false, users: 0, groups: 0 },
        },
      ],
      ...overrides,
    };
  }
  const EXPENSES = item('expenses', {
    app_id: APP.id,
    last_used_at: '2026-10-08T09:00:00Z',
    environments: [
      {
        environment_id: PROD_ENV,
        name: 'prod',
        current_release: { release_id: 'rel_rrrrrrrrrrrrrrrrrrrr', number: 12 },
        last_deploy: { operation_id: 'op_oooooooooooooooooooo', kind: 'deploy', state: 'healthy', at: '2026-10-07T10:00:00Z' },
        sharing: { org_wide: true, users: 2, groups: 1 },
      },
      {
        environment_id: PREVIEW_ENV,
        name: 'preview',
        current_release: { release_id: 'rel_ssssssssssssssssssss', number: 13 },
        last_deploy: { operation_id: 'op_pppppppppppppppppppp', kind: 'rollback', state: 'failed', at: '2026-10-08T10:00:00Z' },
        sharing: { org_wide: false, users: 1, groups: 0 },
      },
    ],
  });
  const TIMESHEETS = item('timesheets', { status: 'quarantined', owner: { user_id: OTHER, display_name: 'Dan Other', name: 'Dan Other', email: 'dan@example.com' } });
  const usage = () =>
    json(200, {
      month: '2026-10',
      fixed_resources: [],
      environments: [{ environment_id: PROD_ENV, app_id: APP.id, billing: 'instance' }, { environment_id: PREVIEW_ENV, app_id: APP.id, billing: null }],
    });
  const cells = (row: HTMLElement) => within(row).getAllByRole('cell').map((c) => c.textContent);

  it('warns of an app with no owner in the owner cell', async () => {
    const ownerless = item('ledger', { owner_state: 'ownerless' });
    start(
      '/',
      { 'GET /v1/inventory': () => json(200, { items: [EXPENSES, ownerless], next_cursor: null }), 'GET /v1/usage': usage },
      signedIn(),
    );
    await screen.findByRole('link', { name: 'ledger' });
    const table = screen.getByRole('table', { name: 'Apps in this organisation' });
    const [expenses, ledger] = within(table).getAllByRole('row').slice(1) as [HTMLElement, HTMLElement];
    expect(cells(ledger)[1]).toBe('Olivia Owner No owner. Assign one.');
    expect(within(ledger).getByText('No owner. Assign one.').classList.contains('badge-warning')).toBe(true);
    expect(within(expenses).queryByText('No owner. Assign one.')).toBeNull();
  });

  it('shows each app on one row: owner, a chip per environment, last use and access', async () => {
    const { api } = start(
      '/',
      { 'GET /v1/inventory': () => json(200, { items: [EXPENSES, TIMESHEETS], next_cursor: null }), 'GET /v1/usage': usage },
      signedIn(),
    );
    await screen.findByRole('link', { name: 'expenses' });
    expect(api.of('GET', '/v1/apps')).toHaveLength(0);
    expect(api.of('GET', '/v1/me/apps')).toHaveLength(0);
    const table = screen.getByRole('table', { name: 'Apps in this organisation' });
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'App',
      'Owner',
      'Production',
      'Preview',
      'Last used',
      'Access',
    ]);
    const [expenses, timesheets] = within(table).getAllByRole('row').slice(1) as [HTMLElement, HTMLElement];
    expect(within(expenses).getByRole('link', { name: 'expenses' }).getAttribute('href')).toBe(`/apps/${APP.id}`);
    expect(within(expenses).getByText(/Olivia Owner/)).toBeTruthy();
    expect(within(expenses).getByTitle('olivia@example.com').textContent).toBe('Olivia Owner');
    expect(within(expenses).queryByText(OWNER)).toBeNull();
    expect(within(expenses).getByRole('img', { name: 'active' })).toBeTruthy();
    expect(within(timesheets).getByText('quarantined')).toBeTruthy();
    const deployed = `Last deploy ${new Date('2026-10-07T10:00:00Z').toLocaleString()}`;
    const prod = cells(expenses)[2]!;
    expect(prod).toContain('Release 12');
    expect(prod).toContain('healthy');
    expect(prod).toContain(deployed);
    await waitFor(() => expect(cells(expenses)[2]).toContain('Instance-billed'));
    // The chip shows the release in the tone of its deploy; the rest is its title, and read out.
    const chip = within(expenses).getByTitle(`${deployed} · Instance-billed`);
    expect(chip.querySelector('.badge-success')?.textContent).toBe('Release 12 healthy');
    expect(within(chip).getByText('healthy').classList.contains('visually-hidden')).toBe(true);
    const preview = cells(expenses)[3]!;
    expect(preview).toContain('Release 13');
    expect(preview).toContain('rollback failed');
    expect(preview).toContain('Billing —');
    // A deploy that is not healthy says so where it can be seen.
    const failed = within(expenses).getByText('rollback failed');
    expect(failed.classList.contains('visually-hidden')).toBe(false);
    expect(failed.closest('.badge-danger')).not.toBeNull();
    // Access is production's, with preview's in the title and read out after it.
    const sharing = cells(expenses)[5]!;
    expect(sharing).toContain('Everyone in the organisation, 2 people, 1 group');
    expect(sharing).toContain('Preview: 1 person');
    expect(
      within(expenses).getByTitle('Production: Everyone in the organisation, 2 people, 1 group · Preview: 1 person'),
    ).toBeTruthy();
    // Last use is how long ago; the exact time is its title.
    const used = within(expenses).getByTitle(new Date('2026-10-08T09:00:00Z').toLocaleString());
    expect(used.tagName).toBe('TIME');
    expect(used.getAttribute('datetime')).toBe('2026-10-08T09:00:00Z');
    expect(used.textContent).toMatch(/ ago$/);
    expect(cells(expenses)[4]).toBe(used.textContent);
    expect(cells(timesheets)[4]).toBe('Not yet');
    expect(cells(timesheets)[2]).toContain('Not deployed yet');
    expect(cells(timesheets)[5]).toContain('Nobody yet');
    expect(cells(timesheets)[3]).toBe('None');
    expect(screen.getByText('2 apps')).toBeTruthy();
    expect(api.of('GET', '/v1/usage')).toHaveLength(1);
  });

  it('filters the loaded apps by owner name, status and environment', async () => {
    start('/', { 'GET /v1/inventory': () => json(200, { items: [EXPENSES, TIMESHEETS], next_cursor: null }), 'GET /v1/usage': usage }, signedIn());
    await screen.findByRole('link', { name: 'expenses' });
    const slugs = listedSlugs;
    fireEvent.change(screen.getByLabelText('Filter apps'), { target: { value: 'dan oth' } });
    expect(slugs()).toEqual(['timesheets']);
    expect(screen.getByText('1 of 2 apps')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Filter apps'), { target: { value: '' } });
    chooseStatus('Active');
    expect(slugs()).toEqual(['expenses']);
    chooseStatus('Quarantined');
    expect(slugs()).toEqual(['timesheets']);
    chooseStatus('Disabled');
    expect(screen.getByText('No app matches the filter.')).toBeTruthy();
    chooseStatus('All');
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'preview' } });
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['App', 'Owner', 'Preview', 'Last used', 'Access']);
    expect(slugs()).toEqual(['expenses', 'timesheets']);
    // With one environment on show, the access is that environment's.
    const expenses = screen.getByRole('link', { name: 'expenses' }).closest('tr')!;
    expect(within(expenses).getAllByRole('cell')[4]?.textContent).toBe('1 person');
  });

  it('loads the next page with the cursor the API returned', async () => {
    const { api } = start(
      '/',
      {
        'GET /v1/inventory': [
          () => json(200, { items: [EXPENSES], next_cursor: 'expenses' }),
          () => json(200, { items: [TIMESHEETS], next_cursor: null }),
        ],
        'GET /v1/usage': usage,
      },
      signedIn(),
    );
    await screen.findByText('1 app, more to load');
    expect(api.of('GET', '/v1/inventory')[0]?.query.has('cursor')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Load more apps' }));
    await screen.findByRole('link', { name: 'timesheets' });
    expect(api.of('GET', '/v1/inventory')[1]?.query.get('cursor')).toBe('expenses');
    expect(screen.getByText('2 apps')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Load more apps' })).toBeNull();
  });

  it('still lists the apps, with a dash for billing, when usage cannot be read', async () => {
    start(
      '/',
      {
        'GET /v1/inventory': () => json(200, { items: [EXPENSES], next_cursor: null }),
        'GET /v1/usage': () => problem(503, 'CELL_UNAVAILABLE', 'The cell is not answering.'),
      },
      signedIn(),
    );
    const row = (await screen.findByRole('link', { name: 'expenses' })).closest('tr')!;
    await waitFor(() => expect(cells(row)[2]).toContain('Billing —'));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says so when there are no apps in the inventory', async () => {
    start('/', { 'GET /v1/inventory': [() => json(200, { items: [], next_cursor: null })], 'GET /v1/usage': usage }, signedIn());
    expect(await screen.findByText(/No apps yet/)).toBeTruthy();
  });

  it('shows the refusal of the inventory and its request id', async () => {
    start('/', { 'GET /v1/inventory': () => problem(403, 'FORBIDDEN', 'Org admins only') }, signedIn());
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Org admins only');
    expect(alert.textContent).toContain('req_test_0001');
  });
});

describe('app detail', () => {
  function routes(put: Handler | Handler[], prod: Handler | Handler[]) {
    return {
      [`GET /v1/apps/${APP.id}`]: () => json(200, APP),
      [`GET ${PREVIEW_GRANTS}`]: () =>
        json(200, { environment_id: 'env_preview0000000000000', grants_version: 0, grants: [] }),
      [`GET ${PROD_GRANTS}`]: prod,
      [`PUT ${PROD_GRANTS}`]: put,
    };
  }

  it('lists production before preview, with who has access to each', async () => {
    start(`/apps/${APP.id}`, routes([], () => json(200, prodGrants(3, [OWNER_BUILDER, ORG_USER]))), signedIn());
    const headings = await screen.findAllByRole('heading', { level: 2 });
    expect(headings.map((h) => h.textContent)).toEqual(['Production prod', 'Preview preview']);
    const prod = screen.getByRole('region', { name: 'Production prod' });
    await within(prod).findByText('Everyone in the organisation, Olive Owner');
    expect(within(prod).queryByText('dep_eeeeeeeeeeeeeeeeeeee')).toBeNull();
    const details = await openSection(prod, 'Details');
    expect(details.textContent).toContain('dep_eeeeeeeeeeeeeeeeeeee');
    expect(within(details).getByRole('button', { name: 'Copy production deployment id' })).toBeTruthy();
    const preview = screen.getByRole('region', { name: 'Preview preview' });
    expect(await within(preview).findByText('Nobody yet')).toBeTruthy();
    expect(within(preview).getAllByText('None yet')).toHaveLength(2);

    const access = await openSection(prod, 'Who has access');
    await within(access).findByText('Everyone in the organisation');
    expect(within(access).getByText('Olive Owner')).toBeTruthy();
    await chooseEnv('Preview');
    expect(await screen.findByText('Nobody has access yet.')).toBeTruthy();

    await openTab('Settings');
    await screen.findByRole('region', { name: 'Admin actions' });
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'Secrets',
      'Timers',
      'Repository',
      'Admin actions',
    ]);
  });

  it('removes access only after the slug is typed, and survives a 412 by re-reading', async () => {
    const { api } = start(
      `/apps/${APP.id}?tab=access`,
      routes(
        [
          () => problem(412, 'PRECONDITION_STALE', 'Changed since you read it'),
          () => json(200, prodGrants(5, [OWNER_BUILDER])),
        ],
        [
          () => json(200, prodGrants(3, [OWNER_BUILDER, ORG_USER])),
          () => json(200, prodGrants(4, [OWNER_BUILDER, ORG_USER]), { ETag: '"4"' }),
          () => json(200, prodGrants(5, [OWNER_BUILDER])),
        ],
      ),
      signedIn(),
    );
    const prod = await screen.findByRole('region', { name: 'Who has access' });
    fireEvent.click(
      await within(prod).findByRole('button', {
        name: 'Remove access for Everyone in the organisation (user) in Production',
      }),
    );
    const dialog = await screen.findByRole('dialog', { name: 'Remove access' });
    const confirm = within(dialog).getByRole('button', { name: 'Remove access' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expense' } });
    expect(confirm.disabled).toBe(true);
    expect(api.of('PUT', PROD_GRANTS)).toHaveLength(0);
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expenses' } });
    expect(confirm.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(confirm);
    });
    expect(await toast()).toBe('Removed access for Everyone in the organisation (user).');
    const puts = api.of('PUT', PROD_GRANTS);
    expect(puts.map((p) => p.headers.get('If-Match'))).toEqual(['"3"', '"4"']);
    expect(puts[1]?.body).toEqual({ grants: [{ role: 'builder', subject_kind: 'user', subject_id: OWNER }] });
    await waitFor(() => expect(within(prod).queryByText('Everyone in the organisation')).toBeNull());
    expect(within(prod).getByText('Olive Owner')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('keeps the dialog open and shows the refusal when removal is refused', async () => {
    start(
      `/apps/${APP.id}?tab=access`,
      routes(() => problem(403, 'FORBIDDEN', 'Only a builder can change sharing'), () =>
        json(200, prodGrants(3, [ORG_USER])),
      ),
      signedIn(),
    );
    const prod = await screen.findByRole('region', { name: 'Who has access' });
    fireEvent.click(await within(prod).findByRole('button', { name: /Remove access for/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Remove access' });
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expenses' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Remove access' }));
    });
    expect((await within(dialog).findByRole('alert')).textContent).toContain('Only a builder can change sharing');
    expect(within(prod).getByText('Everyone in the organisation')).toBeTruthy();
  });

  it('says when a removal waits for approval and keeps the grant listed', async () => {
    const pending = { environment_id: 'env_prod0000000000000000', grants_version: 3, approval_ids: ['apr_aaaaaaaaaaaaaaaaaaaa'] };
    start(
      `/apps/${APP.id}?tab=access`,
      routes(() => json(202, pending, { ETag: '"3"' }), () => json(200, prodGrants(3, [ORG_USER]))),
      signedIn(),
    );
    const prod = await screen.findByRole('region', { name: 'Who has access' });
    fireEvent.click(await within(prod).findByRole('button', { name: /Remove access for/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Remove access' });
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expenses' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Remove access' }));
    });
    expect((await within(prod).findByRole('status')).textContent).toBe(
      'Waiting for approval, nothing changed yet: apr_aaaaaaaaaaaaaaaaaaaa.',
    );
    expect(within(prod).getByText('Everyone in the organisation')).toBeTruthy();
  });

  it('shows not found for an unknown app', async () => {
    start('/apps/app_unknown0000000000000', {}, signedIn());
    expect((await screen.findByRole('alert')).textContent).toContain('Not found');
  });
});
