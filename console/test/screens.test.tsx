import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Grants } from '../src/api/grants';
import { createSession, STORAGE_KEY } from '../src/auth/session';
import { type Handler, json, problem } from './fakeApi';
import { whoami } from './appPage';
import { signedIn, start, WHOAMI } from './harness';

const OWNER = 'usr_cccccccccccccccccccc';
const OTHER = 'usr_dddddddddddddddddddd';
const APP = {
  id: 'app_aaaaaaaaaaaaaaaaaaaa',
  slug: 'expenses',
  owner_user_id: OWNER,
  status: 'active',
  created_at: '2026-09-28T10:00:00Z',
  environments: [
    { id: 'env_preview0000000000000', name: 'preview', config_version: 1, grants_version: 0, current_deployment_id: null },
    { id: 'env_prod0000000000000000', name: 'prod', config_version: 2, grants_version: 3, current_deployment_id: 'dep_eeeeeeeeeeeeeeeeeeee' },
  ],
} as const;
const PROD_GRANTS = `/v1/apps/${APP.id}/environments/env_prod0000000000000000/grants`;
const PREVIEW_GRANTS = `/v1/apps/${APP.id}/environments/env_preview0000000000000/grants`;
const ORG_USER = { id: 'gnt_000000000000000000o1', role: 'user', subject_kind: 'org', subject_id: null } as const;
const OWNER_BUILDER = { id: 'gnt_000000000000000000b1', role: 'builder', subject_kind: 'user', subject_id: OWNER } as const;
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

describe('inventory', () => {
  const apps = [
    { id: 'app_zzzzzzzzzzzzzzzzzzzz', slug: 'timesheets', owner_user_id: OTHER, status: 'quarantined' },
    { id: APP.id, slug: 'expenses', owner_user_id: OWNER, status: 'active' },
  ];

  // Anyone but an org admin reads the plain list; an admin reads the inventory (below).
  const member = { 'GET /v1/whoami': whoami('member') };

  it('lists every app by slug with status and owner, and filters by slug or owner', async () => {
    const { api } = start('/', { ...member, 'GET /v1/apps': () => json(200, { apps }) }, signedIn());
    await screen.findByRole('link', { name: 'expenses' });
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell')[0]?.textContent)).toEqual(['expenses', 'timesheets']);
    expect(within(rows[1]!).getByText('quarantined')).toBeTruthy();
    expect(screen.getByText('2 apps')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Filter apps'), { target: { value: OTHER.slice(0, 10) + 'ddd' } });
    expect(screen.queryByRole('link', { name: 'expenses' })).toBeNull();
    expect(screen.getByText('1 of 2 apps')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Filter apps'), { target: { value: 'nothing' } });
    expect(screen.getByText('No app matches the filter.')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Filter apps'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'quarantined' } });
    expect(screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[0]?.textContent)).toEqual(['timesheets']);
    expect(screen.queryByLabelText('Environment')).toBeNull();
    expect(api.of('GET', '/v1/inventory')).toHaveLength(0);
    expect(api.of('GET', '/v1/usage')).toHaveLength(0);
  });

  it('says so when there are no apps', async () => {
    start('/', { ...member, 'GET /v1/apps': () => json(200, { apps: [] }) }, signedIn());
    expect(await screen.findByText(/No apps yet/)).toBeTruthy();
  });

  it('shows the refusal and its request id', async () => {
    start('/', { ...member, 'GET /v1/apps': () => problem(403, 'FORBIDDEN', 'Not allowed') }, signedIn());
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Not allowed');
    expect(alert.textContent).toContain('req_test_0001');
  });

  it('returns to the login page when the token stops working', async () => {
    const session = signedIn();
    const { router } = start('/', { ...member, 'GET /v1/apps': () => problem(401, 'UNAUTHENTICATED', 'Expired') }, session);
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
      owner: { user_id: OWNER, display_name: 'Olivia Owner' },
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
  const TIMESHEETS = item('timesheets', { status: 'quarantined', owner: { user_id: OTHER, display_name: 'Dan Other' } });
  const usage = () =>
    json(200, {
      month: '2026-10',
      fixed_resources: [],
      environments: [{ environment_id: PROD_ENV, app_id: APP.id, billing: 'instance' }, { environment_id: PREVIEW_ENV, app_id: APP.id, billing: null }],
    });
  const cells = (row: HTMLElement) => within(row).getAllByRole('cell').map((c) => c.textContent);

  it('shows each app with its owner, last use and, per environment, release, last deploy, sharing and billing', async () => {
    const { api } = start(
      '/',
      { 'GET /v1/inventory': () => json(200, { items: [EXPENSES, TIMESHEETS], next_cursor: null }), 'GET /v1/usage': usage },
      signedIn(),
    );
    await screen.findByRole('link', { name: 'expenses' });
    expect(api.of('GET', '/v1/apps')).toHaveLength(0);
    const table = screen.getByRole('table', { name: 'Apps in this organisation' });
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'App',
      'Status',
      'Owner',
      'Last used',
      'Production',
      'Preview',
    ]);
    const [expenses, timesheets] = within(table).getAllByRole('row').slice(1) as [HTMLElement, HTMLElement];
    expect(within(expenses).getByRole('link', { name: 'expenses' }).getAttribute('href')).toBe(`/apps/${APP.id}`);
    expect(within(expenses).getByText(/Olivia Owner/)).toBeTruthy();
    expect(within(expenses).getByTitle(OWNER).textContent).toBe(OWNER);
    const prod = cells(expenses)[4]!;
    expect(prod).toContain('Release 12');
    expect(prod).toContain('healthy');
    expect(prod).toContain(`Last deploy ${new Date('2026-10-07T10:00:00Z').toLocaleString()}`);
    expect(prod).toContain('Everyone in the organisation, 2 people, 1 group');
    await waitFor(() => expect(cells(expenses)[4]).toContain('Instance-billed'));
    const preview = cells(expenses)[5]!;
    expect(preview).toContain('Release 13');
    expect(preview).toContain('rollback failed');
    expect(preview).toContain('1 person');
    expect(preview).toContain('Billing —');
    expect(cells(expenses)[3]).toBe(new Date('2026-10-08T09:00:00Z').toLocaleString());
    expect(cells(timesheets)[3]).toBe('Not yet');
    expect(cells(timesheets)[4]).toContain('Not deployed yet');
    expect(cells(timesheets)[4]).toContain('Nobody yet');
    expect(cells(timesheets)[5]).toBe('None');
    expect(screen.getByText('2 apps')).toBeTruthy();
    expect(api.of('GET', '/v1/usage')).toHaveLength(1);
  });

  it('filters the loaded apps by owner name, status and environment', async () => {
    start('/', { 'GET /v1/inventory': () => json(200, { items: [EXPENSES, TIMESHEETS], next_cursor: null }), 'GET /v1/usage': usage }, signedIn());
    await screen.findByRole('link', { name: 'expenses' });
    const slugs = () => screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[0]?.textContent);
    fireEvent.change(screen.getByLabelText('Filter apps'), { target: { value: 'dan oth' } });
    expect(slugs()).toEqual(['timesheets']);
    expect(screen.getByText('1 of 2 apps')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Filter apps'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'active' } });
    expect(slugs()).toEqual(['expenses']);
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'disabled' } });
    expect(screen.getByText('No app matches the filter.')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'preview' } });
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['App', 'Status', 'Owner', 'Last used', 'Preview']);
    expect(slugs()).toEqual(['expenses', 'timesheets']);
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
    await waitFor(() => expect(cells(row)[4]).toContain('Billing —'));
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
    expect(headings.map((h) => h.textContent)).toEqual(['Production prod', 'Preview preview', 'Repository', 'Admin actions']);
    const prod = screen.getByRole('region', { name: 'Production prod' });
    await within(prod).findByText('Everyone in the organisation');
    expect(within(prod).getByText(OWNER)).toBeTruthy();
    expect(within(prod).getByText('dep_eeeeeeeeeeeeeeeeeeee')).toBeTruthy();
    const preview = screen.getByRole('region', { name: 'Preview preview' });
    expect(await within(preview).findByText('Nobody has access yet.')).toBeTruthy();
    expect(within(preview).getByText('None yet')).toBeTruthy();
  });

  it('removes access only after the slug is typed, and survives a 412 by re-reading', async () => {
    const { api } = start(
      `/apps/${APP.id}`,
      routes(
        [
          () => problem(412, 'PRECONDITION_STALE', 'Changed since you read it'),
          ({ body }) => json(200, prodGrants(5, (body as Grants).grants as Grants['grants'])),
        ],
        [
          () => json(200, prodGrants(3, [OWNER_BUILDER, ORG_USER])),
          () => json(200, prodGrants(4, [OWNER_BUILDER, ORG_USER]), { ETag: '"4"' }),
          () => json(200, prodGrants(5, [OWNER_BUILDER])),
        ],
      ),
      signedIn(),
    );
    const prod = await screen.findByRole('region', { name: 'Production prod' });
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
    expect(await within(prod).findByRole('status')).toBeTruthy();
    const puts = api.of('PUT', PROD_GRANTS);
    expect(puts.map((p) => p.headers.get('If-Match'))).toEqual(['"3"', '"4"']);
    expect(puts[1]?.body).toEqual({ grants: [{ role: 'builder', subject_kind: 'user', subject_id: OWNER }] });
    await waitFor(() => expect(within(prod).queryByText('Everyone in the organisation')).toBeNull());
    expect(within(prod).getByText(OWNER)).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('keeps the dialog open and shows the refusal when removal is refused', async () => {
    start(
      `/apps/${APP.id}`,
      routes(() => problem(403, 'FORBIDDEN', 'Only a builder can change sharing'), () =>
        json(200, prodGrants(3, [ORG_USER])),
      ),
      signedIn(),
    );
    const prod = await screen.findByRole('region', { name: 'Production prod' });
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
      `/apps/${APP.id}`,
      routes(() => json(202, pending, { ETag: '"3"' }), () => json(200, prodGrants(3, [ORG_USER]))),
      signedIn(),
    );
    const prod = await screen.findByRole('region', { name: 'Production prod' });
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
