import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { APP_TAB_TITLE, appSearch } from '../src/app-detail/tabs';
import {
  APP_ID,
  APP_PATH,
  app,
  chooseEnv,
  envPanel,
  FIRST_RENDER_MS,
  openSection,
  openTab,
  ORG_USER,
  page,
  PREVIEW,
  PROD,
  PROD_GRANTS,
} from './appPage';
import { json } from './fakeApi';
import { signedIn, start } from './harness';

const TABS = ['Overview', 'Logs', 'Access', 'Settings'];
const PROD_SECRETS = `${APP_PATH}/environments/${PROD}/secrets`;
const PREVIEW_SECRETS = `${APP_PATH}/environments/${PREVIEW}/secrets`;

function tabs() {
  return screen.findByRole('navigation', { name: 'App sections' }, { timeout: FIRST_RENDER_MS });
}

function current(nav: HTMLElement): (string | null)[] {
  return within(nav)
    .getAllByRole('link')
    .filter((a) => a.getAttribute('aria-current'))
    .map((a) => a.textContent);
}

describe("the app page's search", () => {
  it('keeps a known tab and environment, and leaves the defaults out of the address', () => {
    expect(appSearch({})).toEqual({});
    expect(appSearch({ tab: 'overview', env: 'prod' })).toEqual({});
    expect(appSearch({ tab: 'settings', env: 'preview' })).toEqual({ tab: 'settings', env: 'preview' });
    expect(appSearch({ tab: 'logs' })).toEqual({ tab: 'logs' });
  });

  it('maps a tab from before the merge to the tab it moved to', () => {
    expect(appSearch({ tab: 'data', env: 'preview' })).toEqual({ tab: 'access', env: 'preview' });
    expect(appSearch({ tab: 'secrets' })).toEqual({ tab: 'settings' });
    expect(appSearch({ tab: 'timers' })).toEqual({ tab: 'settings' });
  });

  it('drops a tab or an environment it does not know', () => {
    expect(appSearch({ tab: 'billing', env: 'staging' })).toEqual({});
    expect(appSearch({ tab: 7, env: ['preview'] })).toEqual({});
    expect(appSearch({ tab: 'constructor' })).toEqual({});
    expect(appSearch({ tab: 'logs', env: 'PREVIEW', other: 'x' })).toEqual({ tab: 'logs' });
  });
});

describe("the app page's tabs", () => {
  it('lands an address without a tab on the overview, with both environments side by side', async () => {
    const { router } = start(`/apps/${APP_ID}`, page(), signedIn());
    const nav = await tabs();
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual(TABS);
    expect(current(nav)).toEqual(['Overview']);
    expect(await envPanel('Production')).toBeTruthy();
    expect(await envPanel('Preview')).toBeTruthy();
    expect(screen.queryByRole('navigation', { name: 'Environment' })).toBeNull();
    expect(router.state.location.search).toEqual({});
  });

  it('is a row of links, each to its own address', async () => {
    start(`/apps/${APP_ID}`, page(), signedIn());
    const nav = await tabs();
    expect(within(nav).getAllByRole('link').map((a) => a.getAttribute('href'))).toEqual([
      `/apps/${APP_ID}`,
      `/apps/${APP_ID}?tab=logs`,
      `/apps/${APP_ID}?tab=access`,
      `/apps/${APP_ID}?tab=settings`,
    ]);
  });

  it('opens the tab and the environment the address names', async () => {
    const { api } = start(
      `/apps/${APP_ID}?tab=settings&env=preview`,
      page({ [`GET ${PREVIEW_SECRETS}`]: () => json(200, { environment_id: PREVIEW, items: [] }) }),
      signedIn(),
    );
    const nav = await tabs();
    expect(current(nav)).toEqual(['Settings']);
    const envs = screen.getByRole('navigation', { name: 'Environment' });
    expect(within(envs).getAllByRole('link').map((a) => a.textContent)).toEqual(['Production', 'Preview']);
    expect(current(envs)).toEqual(['Preview']);
    expect(await screen.findByRole('region', { name: 'Secrets' })).toBeTruthy();
    await waitFor(() => expect(api.of('GET', PREVIEW_SECRETS)).toHaveLength(1));
    expect(api.of('GET', PROD_SECRETS)).toHaveLength(0);
    expect(screen.queryByRole('region', { name: 'Production prod' })).toBeNull();
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' });
    await waitFor(() => expect(crumbs.textContent).toBe('AppsexpensesSettings'));
  });

  it('shows the overview for a tab and an environment it does not know', async () => {
    start(`/apps/${APP_ID}?tab=billing&env=staging`, page(), signedIn());
    const nav = await tabs();
    expect(current(nav)).toEqual(['Overview']);
    expect(await envPanel('Production')).toBeTruthy();
    expect(within(nav).getByRole('link', { name: 'Settings' }).getAttribute('href')).toBe(`/apps/${APP_ID}?tab=settings`);
  });

  it('holds the tab in the address, and the environment across tabs', async () => {
    const { router } = start(`/apps/${APP_ID}`, page(), signedIn());
    const nav = await tabs();
    await openTab('Settings');
    expect(router.state.location.search).toEqual({ tab: 'settings' });
    expect(current(nav)).toEqual(['Settings']);
    expect(current(screen.getByRole('navigation', { name: 'Environment' }))).toEqual(['Production']);

    await chooseEnv('Preview');
    expect(router.state.location.search).toEqual({ tab: 'settings', env: 'preview' });
    await openTab('Logs');
    expect(router.state.location.search).toEqual({ tab: 'logs', env: 'preview' });
    expect(current(screen.getByRole('navigation', { name: 'Environment' }))).toEqual(['Preview']);
    expect(within(nav).getByRole('link', { name: 'Access' }).getAttribute('href')).toBe(
      `/apps/${APP_ID}?tab=access&env=preview`,
    );

    await chooseEnv('Production');
    expect(router.state.location.search).toEqual({ tab: 'logs' });
    await openTab('Overview');
    expect(router.state.location.search).toEqual({});
    expect(await envPanel('Preview')).toBeTruthy();
  });

  it('shows the right sections on each tab, the danger zone last', async () => {
    start(`/apps/${APP_ID}`, page(), signedIn());
    const titles = () => screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);
    await tabs();
    await envPanel('Preview');
    expect(titles()).toEqual(['Production prod', 'Preview preview']);
    await openTab('Logs');
    await screen.findByRole('region', { name: 'Logs' });
    expect(titles()).toEqual(['Logs']);
    await openTab('Access');
    await screen.findByRole('region', { name: 'Who has access' });
    expect(titles()).toEqual(['Who has access', 'Data connections', 'Why can this person open it?']);
    await openTab('Settings');
    const danger = await screen.findByRole('region', { name: 'Admin actions' });
    expect(titles()).toEqual(['Secrets', 'Timers', 'Repository', 'Admin actions']);
    expect(danger.classList.contains('danger-zone')).toBe(true);
  });

  it.each([
    ['data', { tab: 'access', env: 'preview' }, 'data-connections', 'Data connections'],
    ['secrets', { tab: 'settings', env: 'preview' }, 'secrets', 'Secrets'],
    ['timers', { tab: 'settings', env: 'preview' }, 'timers', 'Timers'],
  ])('sends an old ?tab=%s address to the tab it moved to', async (old, search, hash, region) => {
    const { router } = start(`/apps/${APP_ID}?tab=${old}&env=preview`, page(), signedIn());
    const nav = await tabs();
    expect(current(nav)).toEqual([APP_TAB_TITLE[search.tab as 'access' | 'settings']]);
    expect(router.state.location.search).toEqual(search);
    expect(router.state.location.hash).toBe(hash);
    expect(await screen.findByRole('region', { name: region })).toBeTruthy();
  });

  it('steps back through the tabs with the back button', async () => {
    const { router } = start(`/apps/${APP_ID}`, page(), signedIn());
    const nav = await tabs();
    await openTab('Access');
    await openTab('Settings');
    expect(current(nav)).toEqual(['Settings']);
    await act(async () => {
      router.history.back();
    });
    await waitFor(() => expect(current(nav)).toEqual(['Access']));
    expect(await screen.findByRole('region', { name: 'Who has access' })).toBeTruthy();
  });
});

describe("the app page's header and overview", () => {
  it('shows the owner and the app id, each with a button that copies it', async () => {
    start(`/apps/${APP_ID}`, page(), signedIn());
    await tabs();
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('expenses');
    expect(screen.getByRole('button', { name: 'Copy app id' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy owner id' })).toBeTruthy();
    expect(screen.getByText(APP_ID)).toBeTruthy();
  });

  it('says who an environment is shared with, and links to who exactly', async () => {
    const { router } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${PROD_GRANTS}`]: () =>
          json(200, {
            environment_id: PROD,
            grants_version: 4,
            grants: [
              ORG_USER,
              { id: 'gnt_000000000000000000u1', role: 'builder', subject_kind: 'user', subject_id: 'usr_dddddddddddddddddddd', subject_name: 'Olive Owner', subject_email: 'olive@example.com' },
              { id: 'gnt_000000000000000000u2', role: 'user', subject_kind: 'user', subject_id: 'usr_eeeeeeeeeeeeeeeeeeee' },
              { id: 'gnt_000000000000000000g1', role: 'user', subject_kind: 'group', subject_id: 'grp_ffffffffffffffffffff' },
            ],
          }),
      }),
      signedIn(),
    );
    const prod = await envPanel('Production');
    expect(await within(prod).findByText('Everyone in the organisation, Olive Owner and 2 more')).toBeTruthy();
    const preview = await envPanel('Preview');
    expect(await within(preview).findByText('Nobody yet')).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(preview).getByRole('link', { name: 'See who has access to Preview' }));
    });
    expect(router.state.location.search).toEqual({ tab: 'access', env: 'preview' });
    expect(current(screen.getByRole('navigation', { name: 'Environment' }))).toEqual(['Preview']);
    expect(await screen.findByText('Nobody has access yet.')).toBeTruthy();
  });

  it('shares from the overview, where the summary then counts the new rule', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`PUT ${PROD_GRANTS}`]: ({ body }) =>
          json(200, {
            environment_id: PROD,
            grants_version: 4,
            grants: (body as { grants: { subject_id?: string }[] }).grants.map((g, i) => ({
              ...g,
              subject_id: g.subject_id ?? null,
              id: `gnt_00000000000000000${String(i).padStart(3, '0')}`,
            })),
          }),
      }),
      signedIn(),
    );
    const prod = await envPanel('Production');
    await within(prod).findByText('Everyone in the organisation');
    fireEvent.click(within(prod).getByRole('button', { name: 'Share Production' }));
    const dialog = await screen.findByRole('dialog', { name: 'Share production' });
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), {
      target: { value: 'grp_ffffffffffffffffffff' },
    });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Share' }));
    });
    await waitFor(() => expect(api.of('PUT', PROD_GRANTS)).toHaveLength(1));
    expect(api.of('PUT', PROD_GRANTS)[0]?.headers.get('If-Match')).toBe('"3"');
    expect(await within(prod).findByText('Everyone in the organisation and 1 more')).toBeTruthy();
  });

  it('gives each card exactly one primary action: Share on production, Promote on preview', async () => {
    start(`/apps/${APP_ID}`, page(), signedIn());
    const prod = await envPanel('Production');
    const preview = await envPanel('Preview');
    const primary = (panel: HTMLElement) =>
      within(panel)
        .getAllByRole('button')
        .filter((b) => b.classList.contains('btn-primary'))
        .map((b) => b.getAttribute('aria-label'));
    expect(primary(prod)).toEqual(['Share Production']);
    expect(primary(preview)).toEqual(['Promote preview to production']);
    expect(within(prod).getByRole('button', { name: 'Roll back Production' }).classList.contains('btn-secondary')).toBe(true);
    expect(within(preview).getByRole('button', { name: 'Roll back Preview' }).classList.contains('btn-secondary')).toBe(true);
    expect(within(preview).queryByRole('button', { name: 'Share Preview' })).toBeNull();
  });

  it('keeps the ids in a Details disclosure, closed until opened', async () => {
    start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}`]: () =>
          json(200, {
            ...app(),
            environments: app().environments.map((e) =>
              e.name === 'prod' ? { ...e, current_deployment_id: 'dep_eeeeeeeeeeeeeeeeeeee' } : e,
            ),
          }),
      }),
      signedIn(),
    );
    const prod = await envPanel('Production');
    const details = within(prod).getByText('Details').closest('details');
    expect(details?.open).toBe(false);
    expect(within(prod).queryByText('dep_eeeeeeeeeeeeeeeeeeee')).toBeNull();
    expect(within(prod).queryByText(PROD)).toBeNull();
    expect(within(prod).queryByText('Config version')).toBeNull();
    const opened = await openSection(prod, 'Details');
    expect(within(opened).getByText('dep_eeeeeeeeeeeeeeeeeeee')).toBeTruthy();
    expect(within(opened).getByText(PROD)).toBeTruthy();
    expect(within(opened).getByText('Config version')).toBeTruthy();
  });

  it('opens the same Share dialog from the production card and from the Access tab', async () => {
    start(`/apps/${APP_ID}`, page(), signedIn());
    const prod = await envPanel('Production');
    fireEvent.click(within(prod).getByRole('button', { name: 'Share Production' }));
    const fromCard = await screen.findByRole('dialog', { name: 'Share production' });
    fireEvent.click(within(fromCard).getByRole('button', { name: 'Cancel' }));
    await openTab('Access');
    const access = await screen.findByRole('region', { name: 'Who has access' });
    expect(screen.getAllByRole('button', { name: 'Share Production' })).toHaveLength(1);
    fireEvent.click(within(access).getByRole('button', { name: 'Share Production' }));
    const fromAccess = await screen.findByRole('dialog', { name: 'Share production' });
    expect(within(fromAccess).getByRole('group', { name: 'Share with' })).toBeTruthy();
  });
});
