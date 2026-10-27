import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { APP_ID, FIRST_RENDER_MS, page } from './appPage';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start, toast, WHOAMI } from './harness';

const PAT = 'usr_pppppppppppppppppppp';
const BOB = 'usr_bbbbbbbbbbbbbbbbbbbb';

const socialWhoami: Handler = async () => {
  const body = (await WHOAMI().json()) as Record<string, unknown>;
  return json(200, { ...body, sign_in_mode: 'social' });
};

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

const people = (...list: unknown[]): Handler => () => json(200, { users: list, next_cursor: null });

async function peoplePage(routes: Record<string, Handler | Handler[]>) {
  const result = start(
    '/people',
    { 'GET /v1/whoami': socialWhoami, 'GET /v1/users': people(user(PAT, 'Pat')), ...routes },
    signedIn(),
  );
  await screen.findByRole('heading', { name: 'People', level: 1 }, { timeout: FIRST_RENDER_MS });
  await screen.findByRole('table', { name: 'People, by name' });
  return result;
}

async function openPerson(name: string) {
  fireEvent.click(await screen.findByRole('button', { name }));
  return screen.findByRole('dialog', { name });
}

describe('people in a social org', () => {
  it('invites by email and reads the roster again', async () => {
    const invited = user(BOB, 'bob@example.com', { email: 'bob@example.com', status: 'invited' });
    const { api } = await peoplePage({
      'GET /v1/users': [people(user(PAT, 'Pat')), people(user(PAT, 'Pat'), invited)],
      'POST /v1/users/invite': () => json(201, invited),
    });
    expect(screen.queryByRole('heading', { name: 'Unmatched logins' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Invite by email' }));
    const dialog = await screen.findByRole('dialog', { name: 'Invite by email' });
    fireEvent.change(within(dialog).getByLabelText('Email'), { target: { value: ' Bob@Example.com ' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
    });
    const [call] = api.of('POST', '/v1/users/invite');
    expect(call?.body).toEqual({ email: 'Bob@Example.com' });
    expect(call?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(await toast()).toBe('Invited bob@example.com. They join the first time they sign in.');
    const cell = await screen.findByRole('cell', { name: 'invited' });
    expect(cell.querySelector('.badge')?.className).toBe('badge badge-info');
  });

  it('shows a refused invite in the dialog', async () => {
    await peoplePage({
      'POST /v1/users/invite': () => problem(409, 'DIRECTORY_MANAGED', 'People here come from your company directory.'),
    });
    fireEvent.click(screen.getByRole('button', { name: 'Invite by email' }));
    const dialog = await screen.findByRole('dialog', { name: 'Invite by email' });
    fireEvent.change(within(dialog).getByLabelText('Email'), { target: { value: 'x@example.com' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
    });
    expect((await within(dialog).findByRole('alert')).textContent).toContain('DIRECTORY_MANAGED');
  });

  it('lists the invited people on their own', async () => {
    const { api } = await peoplePage({});
    fireEvent.click(screen.getByRole('radio', { name: 'Invited' }));
    await waitFor(() => expect(api.of('GET', '/v1/users').at(-1)?.query.get('status')).toBe('invited'));
  });

  it('deactivates once confirmed and reactivates', async () => {
    const gone = user(PAT, 'Pat', { status: 'deactivated' });
    const { api } = await peoplePage({
      [`POST /v1/users/${PAT}/deactivate`]: () => json(200, gone),
      [`POST /v1/users/${PAT}/reactivate`]: () => json(200, user(PAT, 'Pat')),
    });
    const dialog = await openPerson('Pat');
    expect(dialog.textContent).not.toContain('identity provider');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Deactivate' }));
    const confirm = await screen.findByRole('dialog', { name: 'Deactivate Pat' });
    expect(confirm.textContent).toContain('loses access now');
    expect(api.of('POST', `/v1/users/${PAT}/deactivate`)).toHaveLength(0);
    await act(async () => {
      fireEvent.click(within(confirm).getByRole('button', { name: 'Deactivate' }));
    });
    expect(api.of('POST', `/v1/users/${PAT}/deactivate`)).toHaveLength(1);
    expect(await toast()).toBe('Deactivated Pat. Their sessions have ended.');
    await act(async () => {
      fireEvent.click(await within(dialog).findByRole('button', { name: 'Reactivate' }));
    });
    expect(api.of('POST', `/v1/users/${PAT}/reactivate`)).toHaveLength(1);
    expect(await within(dialog).findByRole('button', { name: 'Deactivate' })).toBeDefined();
  });

  it('shows the last-admin refusal in the confirm dialog', async () => {
    await peoplePage({
      [`POST /v1/users/${PAT}/deactivate`]: () => problem(409, 'LAST_ORG_ADMIN', 'The org needs an active admin.'),
    });
    const dialog = await openPerson('Pat');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Deactivate' }));
    const confirm = await screen.findByRole('dialog', { name: 'Deactivate Pat' });
    await act(async () => {
      fireEvent.click(within(confirm).getByRole('button', { name: 'Deactivate' }));
    });
    expect((await within(confirm).findByRole('alert')).textContent).toContain('LAST_ORG_ADMIN');
  });
});

describe('people in a directory org', () => {
  it('has no invite, deactivate or invited filter', async () => {
    start('/people', { 'GET /v1/users': people(user(PAT, 'Pat')), 'GET /v1/unlinked-logins': () => json(200, { unlinked_logins: [] }) }, signedIn());
    await screen.findByRole('table', { name: 'People, by name' }, { timeout: FIRST_RENDER_MS });
    expect(screen.queryByRole('button', { name: 'Invite by email' })).toBeNull();
    expect(screen.queryByRole('radio', { name: 'Invited' })).toBeNull();
    const dialog = await openPerson('Pat');
    expect(within(dialog).queryByRole('button', { name: 'Deactivate' })).toBeNull();
    expect(dialog.textContent).toContain('change them in your identity provider');
  });
});

describe('sharing in a social org', () => {
  it('offers a person or everyone, never a group', async () => {
    start(`/apps/${APP_ID}?tab=access`, page({ 'GET /v1/whoami': socialWhoami }), signedIn());
    const panel = await screen.findByRole('region', { name: 'Who has access' }, { timeout: FIRST_RENDER_MS });
    await waitFor(() => expect(within(panel).queryByText('Loading access…')).toBeNull());
    fireEvent.click(within(panel).getByRole('button', { name: 'Share Production' }));
    const dialog = await screen.findByRole('dialog', { name: 'Share production' });
    const kinds = within(within(dialog).getByRole('group', { name: 'Share with' })).getAllByRole('radio');
    expect(kinds.map((k) => k.closest('label')?.textContent)).toEqual(['A person', 'Everyone in the organisation']);
    expect((kinds[0] as HTMLInputElement).checked).toBe(true);
    expect(within(dialog).queryByLabelText('Group name or grp_ id')).toBeNull();
    expect(within(dialog).getByLabelText('Email or usr_ id')).toBeDefined();
  });
});
