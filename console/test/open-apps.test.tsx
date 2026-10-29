import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PURPOSE_MAX } from '../src/components/PageHeader';
import { APP_ID, FIRST_RENDER_MS, OWNER, PREVIEW, PREVIEW_GRANTS, PROD, PROD_GRANTS, whoami } from './appPage';
import { type Handler, json } from './fakeApi';
import { signedIn, start } from './harness';
import { EXPENSES, LEDGER, LEDGER_APP, mine, rows } from './openAppsFixtures';

async function yourApps(apps: unknown[]) {
  const result = start('/', { 'GET /v1/whoami': whoami('member'), 'GET /v1/me/apps': mine(apps) }, signedIn());
  await screen.findByRole('heading', { name: 'Your apps', level: 1 }, { timeout: FIRST_RENDER_MS });
  return result;
}

describe('your apps (a member)', () => {
  it('lists each shared app with how it is shared and a button per environment it opens', async () => {
    const { api } = await yourApps([EXPENSES, LEDGER_APP]);
    const table = await screen.findByRole('table', { name: 'Apps you can open' });
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['App', 'Access', 'Open']);
    const [expenses, ledger] = rows(table) as [HTMLElement, HTMLElement];
    const name = within(expenses).getAllByRole('cell')[0]!;
    expect(name.textContent).toBe('expenses');
    expect(within(name).queryByRole('link')).toBeNull();
    expect(within(expenses).getByText('Everyone in the organisation')).toBeTruthy();
    expect(within(expenses).queryByText('Builder')).toBeNull();
    const open = within(expenses).getAllByRole('link');
    expect(open.map((a) => a.textContent)).toEqual(['Open production expenses']);
    expect(open[0]?.getAttribute('href')).toBe(EXPENSES.environments[0]?.url);
    expect(open[0]?.getAttribute('rel')).toBe('noopener');
    expect(open[0]?.getAttribute('target')).toBe('_blank');
    expect(open[0]?.className).toContain('btn-primary');

    expect(within(ledger).getByRole('link', { name: 'ledger' }).getAttribute('href')).toBe(`/apps/${LEDGER}`);
    expect(within(ledger).getAllByRole('cell')[1]?.textContent).toBe('Group Finance, Shared with youBuilder');
    expect(within(ledger).getByText('Builder').classList.contains('badge')).toBe(true);
    const buttons = within(ledger).getAllByRole('link', { name: /^Open / });
    expect(buttons.map((a) => [a.textContent, a.className.includes('btn-secondary')])).toEqual([
      ['Open production ledger', false],
      ['Open preview ledger', true],
    ]);
    expect(buttons[1]?.getAttribute('href')).toBe(LEDGER_APP.environments[1]?.url);
    expect(screen.queryByText('timesheets')).toBeNull();
    expect(api.of('GET', '/v1/apps')).toHaveLength(0);
    expect(api.of('GET', '/v1/inventory')).toHaveLength(0);
    expect(screen.queryByRole('group', { name: 'Show' })).toBeNull();
  });

  it('marks an app that needs an owner and offers no way in', async () => {
    await yourApps([{ ...LEDGER_APP, owner_state: 'blocked' }, EXPENSES]);
    const table = await screen.findByRole('table', { name: 'Apps you can open' });
    const [ledger, expenses] = rows(table) as [HTMLElement, HTMLElement];
    expect(within(ledger).getByText('Needs an owner').classList.contains('badge-danger')).toBe(true);
    expect(within(ledger).queryAllByRole('link', { name: /^Open / })).toHaveLength(0);
    expect(within(expenses).getAllByRole('link', { name: /^Open / })).toHaveLength(1);
  });

  it('filters by slug or access, counts, says why it is empty and explains the page', async () => {
    await yourApps([EXPENSES, LEDGER_APP]);
    await screen.findByRole('table', { name: 'Apps you can open' });
    expect(screen.getByText('2 apps')).toBeTruthy();
    const filter = screen.getByLabelText('Filter apps');
    expect(filter.getAttribute('placeholder')).toBe('Filter by slug or access');
    fireEvent.change(filter, { target: { value: 'finance' } });
    expect(rows(screen.getByRole('table')).map((r) => r.textContent)).toEqual([expect.stringContaining('ledger')]);
    expect(screen.getByText('1 of 2 apps')).toBeTruthy();
    fireEvent.change(filter, { target: { value: 'expen' } });
    expect(rows(screen.getByRole('table')).map((r) => r.textContent)).toEqual([expect.stringContaining('expenses')]);
    fireEvent.change(filter, { target: { value: 'nothing' } });
    expect(screen.getByText('No app matches the filter.')).toBeTruthy();
    const purpose = document.querySelector('.page-purpose')?.textContent ?? '';
    expect(purpose.length).toBeGreaterThan(0);
    expect(purpose.length).toBeLessThanOrEqual(PURPOSE_MAX);
    expect(screen.getByText('About this page').closest('details')).toBeTruthy();
  });

  it('says no app is shared yet, and who to ask', async () => {
    await yourApps([]);
    expect(await screen.findByText('No apps are shared with you yet.')).toBeTruthy();
    expect(screen.getByText("Ask the app's owner or an org admin to share one with you.")).toBeTruthy();
    expect(screen.getByText('0 apps')).toBeTruthy();
  });
});

const INVENTORY_ITEM = {
  app_id: APP_ID,
  slug: 'expenses',
  status: 'active',
  created_at: '2026-09-28T10:00:00Z',
  owner_state: 'active',
  owner: { user_id: OWNER, display_name: 'Olive Owner', name: 'Olive Owner', email: 'olive@example.com' },
  last_used_at: null,
  environments: [
    { environment_id: PROD, name: 'prod', current_release: null, last_deploy: null, sharing: { org_wide: true, users: 0, groups: 1 } },
    { environment_id: PREVIEW, name: 'preview', current_release: null, last_deploy: null, sharing: { org_wide: false, users: 1, groups: 0 } },
  ],
};
const GRANT_PROD = [
  { id: 'gnt_000000000000000000o1', role: 'user', subject_kind: 'org', subject_id: null, subject_name: null, subject_email: null },
  { id: 'gnt_000000000000000000g1', role: 'user', subject_kind: 'group', subject_id: 'grp_ffffffffffffffffffff', subject_name: 'Finance', subject_email: null },
];
const GRANT_PREVIEW = [
  { id: 'gnt_000000000000000000b1', role: 'builder', subject_kind: 'user', subject_id: OWNER, subject_name: 'Olive Owner', subject_email: 'olive@example.com' },
];

function adminPage(routes: Record<string, Handler | Handler[]> = {}) {
  return start(
    '/',
    {
      'GET /v1/whoami': whoami('admin'),
      'GET /v1/inventory': () => json(200, { items: [INVENTORY_ITEM], next_cursor: null }),
      'GET /v1/usage': () => json(200, { month: '2026-10', fixed_resources: [], environments: [] }),
      [`GET ${PROD_GRANTS}`]: () => json(200, { environment_id: PROD, grants_version: 2, grants: GRANT_PROD }),
      [`GET ${PREVIEW_GRANTS}`]: () => json(200, { environment_id: PREVIEW, grants_version: 2, grants: GRANT_PREVIEW }),
      ...routes,
    },
    signedIn(),
  );
}

function show(name: 'All apps' | 'Apps I can open') {
  fireEvent.click(within(screen.getByRole('group', { name: 'Show' })).getByRole('radio', { name }));
}

describe('apps for an org admin', () => {
  it('switches between the inventory and the apps the admin may open', async () => {
    const { api } = adminPage({ 'GET /v1/me/apps': mine([LEDGER_APP]) });
    await screen.findByRole('table', { name: 'Apps in this organisation' }, { timeout: FIRST_RENDER_MS });
    const group = screen.getByRole('group', { name: 'Show' });
    expect(within(group).getAllByRole('radio').map((r) => r.closest('label')?.textContent)).toEqual(['All apps', 'Apps I can open']);
    expect(screen.getByLabelText('Environment')).toBeTruthy();
    expect(api.of('GET', '/v1/me/apps')).toHaveLength(0);
    show('Apps I can open');
    const table = await screen.findByRole('table', { name: 'Apps you can open' });
    expect(rows(table).map((r) => within(r).getAllByRole('cell')[0]?.textContent)).toEqual(['ledger']);
    expect(screen.queryByLabelText('Environment')).toBeNull();
    expect(screen.queryByRole('group', { name: 'Status' })).toBeNull();
    expect(api.of('GET', '/v1/me/apps')).toHaveLength(1);
    show('All apps');
    expect(await screen.findByRole('table', { name: 'Apps in this organisation' })).toBeTruthy();
  });

  it('says why an admin with no grant may open nothing', async () => {
    adminPage({ 'GET /v1/me/apps': mine([]) });
    await screen.findByRole('table', { name: 'Apps in this organisation' }, { timeout: FIRST_RENDER_MS });
    show('Apps I can open');
    expect(
      await screen.findByText('Admins see every app here but open only the ones shared with them, like everyone else.'),
    ).toBeTruthy();
  });

  it('opens who can use an app from its Access cell, by name and role, and closes it', async () => {
    adminPage();
    const table = await screen.findByRole('table', { name: 'Apps in this organisation' }, { timeout: FIRST_RENDER_MS });
    const access = within(table).getByRole('button', { name: /^Everyone in the organisation, 1 group/ });
    expect(access.getAttribute('aria-haspopup')).toBe('dialog');
    fireEvent.click(access);
    const dialog = await screen.findByRole('dialog', { name: 'Who can use expenses' });
    const prod = within(dialog).getByRole('region', { name: 'Production' });
    await within(prod).findByText('Group Finance');
    expect(within(prod).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Everyone in the organisationuser',
      'Group Financeuser',
    ]);
    const preview = within(dialog).getByRole('region', { name: 'Preview' });
    expect(await within(preview).findByText('User Olive Owner')).toBeTruthy();
    expect(within(preview).getByText('builder')).toBeTruthy();
    expect(dialog.textContent).not.toContain(OWNER);
    const change = within(dialog).getByRole('link', { name: 'Change on the Access tab' });
    expect(change.getAttribute('href')).toBe(`/apps/${APP_ID}?tab=access`);
    // Escape closes a native dialog by firing its close event.
    act(() => (dialog as HTMLDialogElement).close());
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Who can use expenses' })).toBeNull());
  });
});
