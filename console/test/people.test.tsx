import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FIRST_RENDER_MS, whoami } from './appPage';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start, toast } from './harness';

const PAT = 'usr_pppppppppppppppppppp';
const SAM = 'usr_ssssssssssssssssssss';

function login(id: string, extra: Record<string, unknown> = {}) {
  return {
    id: `ulg_${id.padEnd(20, '0').slice(0, 20)}`,
    connection_id: 'conn_01',
    subject: `subject-${id}`,
    email: `${id}@example.com`,
    reason: 'no_match',
    attempts: 3,
    first_seen_at: '2026-10-01T09:00:00Z',
    last_seen_at: '2026-10-08T17:30:00Z',
    linkable: true,
    ...extra,
  };
}

const PAT_LOGIN = login('pat');
const SAML_LOGIN = login('sam', { reason: 'ambiguous_email', attempts: 1, linkable: false, subject: 'sam@example.com' });

function user(id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    display_name: name,
    email: `${name.toLowerCase()}@example.com`,
    role: 'member',
    status: 'active',
    created_at: '2026-09-01T09:00:00Z',
    last_sign_in_at: null,
    ...extra,
  };
}

const people = (list: unknown[], next: string | null = null): Handler => () =>
  json(200, { users: list, next_cursor: next });

const logins = (...list: unknown[]): Handler => () => json(200, { unlinked_logins: list });

async function peoplePage(routes: Record<string, Handler | Handler[]>, role: 'admin' | 'member' = 'admin') {
  const result = start(
    '/people',
    {
      'GET /v1/whoami': whoami(role),
      'GET /v1/unlinked-logins': logins(PAT_LOGIN, SAML_LOGIN),
      'GET /v1/users': people([user(PAT, 'Pat')]),
      ...routes,
    },
    signedIn(),
  );
  await screen.findByRole('heading', { name: 'People', level: 1 }, { timeout: FIRST_RENDER_MS });
  return result;
}

describe('people page', () => {
  it('is for org admins: anyone else gets a line, no link in the nav and no read', async () => {
    const { api } = await peoplePage({}, 'member');
    expect(await screen.findByText('Only org admins can look people up and link logins.')).toBeDefined();
    expect(within(screen.getByRole('navigation', { name: 'Main' })).queryByRole('link', { name: 'People' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Unmatched logins' })).toBeNull();
    expect(api.of('GET', '/v1/unlinked-logins')).toHaveLength(0);
    expect(api.of('GET', '/v1/users')).toHaveLength(0);
  });

  it('lists the unmatched logins in the words of ssc logins list', async () => {
    await peoplePage({});
    const table = await screen.findByRole('table', { name: /Unmatched logins/ });
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Email',
      'Why',
      'Attempts',
      'First seen',
      'Last seen',
      'Linkable',
    ]);
    const [, pat, sam] = within(table).getAllByRole('row');
    expect(pat?.textContent).toContain('pat@example.com');
    expect(pat?.textContent).toContain('no one with this email');
    expect(within(pat!).getAllByRole('cell')[2]?.textContent).toBe('3');
    expect(pat?.querySelectorAll('time')[0]?.getAttribute('datetime')).toBe('2026-10-01T09:00:00Z');
    expect(pat?.querySelectorAll('time')[1]?.getAttribute('datetime')).toBe('2026-10-08T17:30:00Z');
    expect(within(pat!).getByRole('button', { name: 'Link pat@example.com to a person' })).toBeDefined();
    // An address-shaped subject cannot be linked: no action, and the reason why.
    expect(sam?.textContent).toContain('several people, same email');
    expect(sam?.textContent).toContain('no: fix it in the directory');
    expect(within(sam!).queryByRole('button')).toBeNull();
  });

  it('says so when every login matched a person', async () => {
    await peoplePage({ 'GET /v1/unlinked-logins': logins() });
    expect(await screen.findByText('Every login so far matched a person.')).toBeDefined();
  });

  it('shows the API refusing the list', async () => {
    await peoplePage({ 'GET /v1/unlinked-logins': () => problem(403, 'FORBIDDEN', 'Org admins only') });
    expect((await screen.findByRole('alert')).textContent).toContain('Org admins only');
  });

  it('links a login to the active person found by exact email', async () => {
    const { api } = await peoplePage({
      'GET /v1/unlinked-logins': [logins(PAT_LOGIN, SAML_LOGIN), logins(SAML_LOGIN)],
      [`POST /v1/unlinked-logins/${PAT_LOGIN.id}/link`]: () =>
        json(200, { identity_link_id: 'idl_1', user_id: PAT, user_name: 'Pat', user_email: 'pat@example.com' }),
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Link pat@example.com to a person' }));
    const dialog = await screen.findByRole('dialog', { name: 'Link pat@example.com to a person' });
    expect(dialog.textContent).toContain('will sign in as the person you pick');
    const link = within(dialog).getByRole('button', { name: 'Link' }) as HTMLButtonElement;
    expect(link.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText('The person: email or usr_ id'), {
      target: { value: 'Pat@Example.com' },
    });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    });
    expect(api.of('GET', '/v1/users').at(-1)?.query.get('email')).toBe('Pat@Example.com');
    expect((await within(dialog).findByRole('radio', { name: /Pat/ }) as HTMLInputElement).checked).toBe(true);
    expect(link.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(link);
    });
    const [call] = api.of('POST', `/v1/unlinked-logins/${PAT_LOGIN.id}/link`);
    expect(call?.body).toEqual({ user_id: PAT });
    expect(call?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(await toast()).toBe('Linked pat@example.com to Pat. Their next login signs them in.');
    // The list is read again and the linked login is gone from it.
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Link pat@example.com to a person' })).toBeNull());
    expect(screen.queryByRole('dialog', { name: 'Link pat@example.com to a person' })).toBeNull();
  });

  it('links by usr_ id with no lookup', async () => {
    const { api } = await peoplePage({
      [`POST /v1/unlinked-logins/${PAT_LOGIN.id}/link`]: () =>
        json(200, { identity_link_id: 'idl_1', user_id: PAT, user_name: 'Pat Lee', user_email: 'pat@example.com' }),
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Link pat@example.com to a person' }));
    const dialog = await screen.findByRole('dialog', { name: 'Link pat@example.com to a person' });
    fireEvent.change(within(dialog).getByLabelText('The person: email or usr_ id'), { target: { value: PAT } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Link' }));
    });
    expect(api.of('GET', '/v1/users').filter((c) => c.query.has('email'))).toHaveLength(0);
    expect(await toast()).toBe('Linked pat@example.com to Pat Lee. Their next login signs them in.');
  });

  it('does not let a deactivated person be picked', async () => {
    const { api } = await peoplePage({
      'GET /v1/users': people([user(PAT, 'Pat', { status: 'deactivated' })]),
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Link pat@example.com to a person' }));
    const dialog = await screen.findByRole('dialog', { name: 'Link pat@example.com to a person' });
    fireEvent.change(within(dialog).getByLabelText('The person: email or usr_ id'), {
      target: { value: 'pat@example.com' },
    });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    });
    const choice = (await within(dialog).findByRole('radio', { name: /Pat/ })) as HTMLInputElement;
    expect(choice.disabled).toBe(true);
    expect(dialog.textContent).toContain('deactivated');
    expect((within(dialog).getByRole('button', { name: 'Link' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.of('POST', `/v1/unlinked-logins/${PAT_LOGIN.id}/link`)).toHaveLength(0);
  });

  it('shows a refused link in the dialog and keeps the login listed', async () => {
    await peoplePage({
      [`POST /v1/unlinked-logins/${PAT_LOGIN.id}/link`]: () =>
        problem(422, 'REFERENCE_NOT_FOUND', 'That person is not active'),
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Link pat@example.com to a person' }));
    const dialog = await screen.findByRole('dialog', { name: 'Link pat@example.com to a person' });
    fireEvent.change(within(dialog).getByLabelText('The person: email or usr_ id'), { target: { value: SAM } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Link' }));
    });
    const alert = await within(dialog).findByRole('alert');
    expect(alert.textContent).toContain('REFERENCE_NOT_FOUND');
    expect(alert.textContent).toContain('That person is not active');
    expect(screen.queryByRole('status')).toBeNull();
  });
});

describe('the roster', () => {
  const ROSTER = 'People, by name';

  async function roster(routes: Record<string, Handler | Handler[]> = {}) {
    const result = await peoplePage(routes);
    const table = await screen.findByRole('table', { name: ROSTER });
    return { ...result, table };
  }

  it('lists people by name, never by id, active ones first asked for', async () => {
    const { api, table } = await roster({
      'GET /v1/users': people([
        user(PAT, 'Pat', { role: 'admin', last_sign_in_at: '2026-10-08T17:30:00Z' }),
        user(SAM, 'Sam'),
      ]),
    });
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Name',
      'Email',
      'Role',
      'State',
      'Last sign-in',
    ]);
    const [, pat, sam] = within(table).getAllByRole('row');
    expect(within(pat!).getAllByRole('cell').slice(0, 4).map((c) => c.textContent)).toEqual([
      'Pat',
      'pat@example.com',
      'admin',
      'active',
    ]);
    expect(pat?.querySelector('time')?.getAttribute('datetime')).toBe('2026-10-08T17:30:00Z');
    expect(within(sam!).getAllByRole('cell')[4]?.textContent).toBe('Never');
    expect(table.textContent).not.toContain('usr_');
    const [call] = api.of('GET', '/v1/users');
    expect(call?.query.get('status')).toBe('active');
    expect(call?.query.has('q')).toBe(false);
  });

  it('searches on the server and switches to everyone', async () => {
    const { api } = await roster({
      'GET /v1/users': [
        people([user(PAT, 'Pat')]),
        people([user(PAT, 'Pat')]),
        people([user(SAM, 'Sam', { status: 'deactivated' })]),
      ],
    });
    fireEvent.change(screen.getByLabelText('Search people'), { target: { value: ' pat ' } });
    await waitFor(() => expect(api.of('GET', '/v1/users')).toHaveLength(2));
    expect(api.of('GET', '/v1/users')[1]?.query.get('q')).toBe('pat');
    fireEvent.click(screen.getByRole('radio', { name: 'All' }));
    await waitFor(() => expect(api.of('GET', '/v1/users')).toHaveLength(3));
    const last = api.of('GET', '/v1/users')[2];
    expect(last?.query.has('status')).toBe(false);
    expect(last?.query.get('q')).toBe('pat');
    const cell = await screen.findByRole('cell', { name: 'deactivated' });
    expect(cell.querySelector('.badge')?.className).toBe('badge badge-neutral');
  });

  it('says when no one matches the search', async () => {
    await roster({ 'GET /v1/users': [people([user(PAT, 'Pat')]), people([])] });
    fireEvent.change(screen.getByLabelText('Search people'), { target: { value: 'zed' } });
    expect(await screen.findByText('No one matches zed.')).toBeDefined();
  });

  it('opens a person with their id and how to change them', async () => {
    const { table } = await roster();
    fireEvent.click(within(table).getByRole('button', { name: 'Pat' }));
    const dialog = await screen.findByRole('dialog', { name: 'Pat' });
    expect(dialog.textContent).toContain('pat@example.com');
    expect(dialog.textContent).toContain('Never');
    expect(dialog.textContent).toContain(PAT);
    expect(within(dialog).getByRole('button', { name: 'Copy person id' })).toBeDefined();
    expect(dialog.textContent).toContain('change them in your identity provider');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Pat' })).toBeNull());
  });

  it('loads the next page from the cursor', async () => {
    const { api } = await roster({
      'GET /v1/users': [people([user(PAT, 'Pat')], PAT), people([user(SAM, 'Sam')])],
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Load more people' }));
    });
    await screen.findByRole('button', { name: 'Sam' });
    expect(api.of('GET', '/v1/users')[1]?.query.get('cursor')).toBe(PAT);
    expect(screen.queryByRole('button', { name: 'Load more people' })).toBeNull();
  });

  it('says people come from the directory sync when there are none', async () => {
    await peoplePage({ 'GET /v1/users': people([]) });
    expect(await screen.findByText('No active people yet.')).toBeDefined();
    expect(screen.getByText(/People appear here after the directory sync/)).toBeDefined();
    const region = screen.getByRole('region', { name: 'People' });
    expect(within(region).getByRole('link', { name: 'Help' }).getAttribute('href')).toBe('/help');
  });
});
