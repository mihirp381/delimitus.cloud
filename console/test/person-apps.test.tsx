import { fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FIRST_RENDER_MS, whoami } from './appPage';
import { json } from './fakeApi';
import { signedIn, start } from './harness';
import { EXPENSES, LEDGER_APP, mine, PAT, rows } from './openAppsFixtures';

function person(status: 'active' | 'invited' | 'deactivated') {
  return {
    id: PAT,
    display_name: 'Pat',
    email: 'pat@example.com',
    role: 'member',
    status,
    created_at: '2026-09-01T09:00:00Z',
    last_sign_in_at: null,
  };
}

async function openPerson(status: 'active' | 'invited' | 'deactivated', apps: unknown[], role: 'admin' | 'member' = 'admin') {
  const result = start(
    '/people',
    {
      'GET /v1/whoami': whoami(role),
      'GET /v1/unlinked-logins': () => json(200, { unlinked_logins: [] }),
      'GET /v1/users': () => json(200, { users: [person(status)], next_cursor: null }),
      [`GET /v1/users/${PAT}/apps`]: mine(apps, PAT),
    },
    signedIn(),
  );
  await screen.findByRole('heading', { name: 'People', level: 1 }, { timeout: FIRST_RENDER_MS });
  return result;
}

describe('apps a person can open', () => {
  it("lists each app and environment the person may open, with the role and how it reaches them", async () => {
    const { api } = await openPerson('active', [EXPENSES, LEDGER_APP]);
    fireEvent.click(await screen.findByRole('button', { name: 'Pat' }));
    const dialog = await screen.findByRole('dialog', { name: 'Pat' });
    const panel = within(dialog).getByRole('region', { name: 'Apps this person can open' });
    const table = await within(panel).findByRole('table', { name: 'Apps this person can open' });
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['App', 'Environment', 'Role', 'Via']);
    expect(rows(table).map((r) => within(r).getAllByRole('cell').map((c) => c.textContent))).toEqual([
      ['expenses', 'Production', 'user', 'Everyone in the organisation'],
      ['ledger', 'Production', 'user', 'Group Finance'],
      ['ledger', 'Preview', 'builder', 'Shared with this person'],
    ]);
    expect(api.of('GET', `/v1/users/${PAT}/apps`)).toHaveLength(1);
  });

  it('says when nothing is shared with them', async () => {
    await openPerson('active', []);
    fireEvent.click(await screen.findByRole('button', { name: 'Pat' }));
    const dialog = await screen.findByRole('dialog', { name: 'Pat' });
    expect(await within(dialog).findByText('Nothing is shared with this person yet.')).toBeTruthy();
  });

  it.each(['deactivated', 'invited'] as const)('says a %s person cannot open any app, and does not ask', async (status) => {
    const { api } = await openPerson(status, [EXPENSES]);
    fireEvent.click(await screen.findByRole('button', { name: 'Pat' }));
    const dialog = await screen.findByRole('dialog', { name: 'Pat' });
    expect(within(dialog).getByText('A person who is not active cannot open any app.')).toBeTruthy();
    expect(within(dialog).queryByRole('table')).toBeNull();
    expect(api.of('GET', `/v1/users/${PAT}/apps`)).toHaveLength(0);
  });

  it('is for org admins only: a member never sees the panel or asks for it', async () => {
    const { api } = await openPerson('active', [EXPENSES], 'member');
    expect(await screen.findByText('Only org admins can look people up and link logins.')).toBeTruthy();
    expect(screen.queryByText('Apps this person can open')).toBeNull();
    expect(api.of('GET', `/v1/users/${PAT}/apps`)).toHaveLength(0);
  });
});
