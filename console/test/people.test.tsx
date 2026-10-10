import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FIRST_RENDER_MS, whoami } from './appPage';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start } from './harness';

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
  return { id, display_name: name, email: `${name.toLowerCase()}@example.com`, role: 'member', status: 'active', ...extra };
}

const logins = (...list: unknown[]): Handler => () => json(200, { unlinked_logins: list });

async function peoplePage(routes: Record<string, Handler | Handler[]>, role: 'admin' | 'member' = 'admin') {
  const result = start(
    '/people',
    { 'GET /v1/whoami': whoami(role), 'GET /v1/unlinked-logins': logins(PAT_LOGIN, SAML_LOGIN), ...routes },
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
  });

  it('says the directory is managed in the identity provider', async () => {
    await peoplePage({});
    await screen.findByRole('table', { name: /Unmatched logins/ });
    expect(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'People' })).toBeDefined();
    expect(document.querySelector('.page-purpose')?.textContent).toContain(
      'People and groups are managed in your identity provider',
    );
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
      'GET /v1/users': () => json(200, { users: [user(PAT, 'Pat')] }),
      [`POST /v1/unlinked-logins/${PAT_LOGIN.id}/link`]: () => json(200, { identity_link_id: 'idl_1', user_id: PAT }),
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
    expect(api.of('GET', '/v1/users')[0]?.query.get('email')).toBe('Pat@Example.com');
    expect((await within(dialog).findByRole('radio', { name: /Pat/ }) as HTMLInputElement).checked).toBe(true);
    expect(link.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(link);
    });
    const [call] = api.of('POST', `/v1/unlinked-logins/${PAT_LOGIN.id}/link`);
    expect(call?.body).toEqual({ user_id: PAT });
    expect(call?.headers.get('Idempotency-Key')).toBeTruthy();
    expect((await screen.findByRole('status')).textContent).toBe(
      `Linked pat@example.com to Pat (${PAT}). Their next login signs them in.`,
    );
    // The list is read again and the linked login is gone from it.
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Link pat@example.com to a person' })).toBeNull());
    expect(screen.queryByRole('dialog', { name: 'Link pat@example.com to a person' })).toBeNull();
  });

  it('links by usr_ id with no lookup', async () => {
    const { api } = await peoplePage({
      [`POST /v1/unlinked-logins/${PAT_LOGIN.id}/link`]: () => json(200, { identity_link_id: 'idl_1', user_id: PAT }),
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Link pat@example.com to a person' }));
    const dialog = await screen.findByRole('dialog', { name: 'Link pat@example.com to a person' });
    fireEvent.change(within(dialog).getByLabelText('The person: email or usr_ id'), { target: { value: PAT } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Link' }));
    });
    expect(api.of('GET', '/v1/users')).toHaveLength(0);
    expect((await screen.findByRole('status')).textContent).toBe(
      `Linked pat@example.com to ${PAT}. Their next login signs them in.`,
    );
  });

  it('does not let a deactivated person be picked', async () => {
    const { api } = await peoplePage({
      'GET /v1/users': () => json(200, { users: [user(PAT, 'Pat', { status: 'deactivated' })] }),
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

describe('looking a person or a group up', () => {
  async function find(form: string, label: string, text: string) {
    const element = await screen.findByRole('form', { name: form });
    fireEvent.change(within(element).getByLabelText(label), { target: { value: text } });
    await act(async () => {
      fireEvent.submit(element);
    });
  }

  it('shows a person by exact email: name, role, status and id, read-only', async () => {
    const { api } = await peoplePage({
      'GET /v1/users': () =>
        json(200, {
          users: [user(PAT, 'Pat', { role: 'admin' }), user(SAM, 'Sam', { email: 'pat@example.com', status: 'deactivated' })],
        }),
    });
    await find('Look up a person', 'Email address', ' pat@example.com ');
    const table = await screen.findByRole('table', { name: 'People with the email pat@example.com' });
    expect(api.of('GET', '/v1/users')[0]?.query.get('email')).toBe('pat@example.com');
    const [, pat, sam] = within(table).getAllByRole('row');
    expect(within(pat!).getAllByRole('cell').map((c) => c.textContent)).toEqual([
      'Pat',
      'pat@example.com',
      'admin',
      'active',
      PAT,
    ]);
    expect(within(sam!).getAllByRole('cell').map((c) => c.textContent)).toEqual([
      'Sam',
      'pat@example.com',
      'member',
      'deactivated',
      SAM,
    ]);
    expect(sam?.querySelector('.badge-danger')?.textContent).toBe('deactivated');
    // Nothing here changes a person.
    const section = screen.getByRole('region', { name: 'Look up a person' });
    expect(within(section).getAllByRole('button').map((b) => b.textContent)).toEqual(['Find']);
    expect(section.textContent).toContain('change them in your identity provider');
  });

  it('says when no one has the address, and does not ask for half an address', async () => {
    const { api } = await peoplePage({ 'GET /v1/users': () => json(200, { users: [] }) });
    await find('Look up a person', 'Email address', 'nobody@example.com');
    expect(await screen.findByText('No one in the organisation has the address nobody@example.com.')).toBeDefined();
    await find('Look up a person', 'Email address', 'nobody');
    const section = screen.getByRole('region', { name: 'Look up a person' });
    expect((await within(section).findByRole('alert')).textContent).toContain('Type a whole email address.');
    expect(api.of('GET', '/v1/users')).toHaveLength(1);
  });

  it('shows a group by exact name: id and active members', async () => {
    const { api } = await peoplePage({
      'GET /v1/groups': () =>
        json(200, { groups: [{ id: 'grp_ffffffffffffffffffff', name: 'Finance', member_count: 3 }] }),
    });
    await find('Look up a group', 'Group name', 'finance');
    const table = await screen.findByRole('table', { name: 'Groups named finance' });
    expect(api.of('GET', '/v1/groups')[0]?.query.get('name')).toBe('finance');
    expect(within(within(table).getAllByRole('row')[1]!).getAllByRole('cell').map((c) => c.textContent)).toEqual([
      'Finance',
      '3 active members',
      'grp_ffffffffffffffffffff',
    ]);
    const section = screen.getByRole('region', { name: 'Look up a group' });
    expect(within(section).getAllByRole('button').map((b) => b.textContent)).toEqual(['Find']);
    expect(section.textContent).toContain('change it in your identity provider');
  });

  it('says when no group has the name, and shows a refusal', async () => {
    await peoplePage({
      'GET /v1/groups': [() => json(200, { groups: [] }), () => problem(403, 'FORBIDDEN', 'Not for agents')],
    });
    await find('Look up a group', 'Group name', 'Nobody');
    expect(await screen.findByText('No group is named exactly Nobody.')).toBeDefined();
    await find('Look up a group', 'Group name', 'Nobody2');
    const section = screen.getByRole('region', { name: 'Look up a group' });
    expect((await within(section).findByRole('alert')).textContent).toContain('Not for agents');
  });
});
