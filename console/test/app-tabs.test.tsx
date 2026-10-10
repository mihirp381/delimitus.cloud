import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { appSearch } from '../src/app-detail/tabs';
import {
  APP_ID,
  APP_PATH,
  chooseEnv,
  envPanel,
  FIRST_RENDER_MS,
  openTab,
  ORG_USER,
  page,
  PREVIEW,
  PROD,
  PROD_GRANTS,
} from './appPage';
import { json } from './fakeApi';
import { signedIn, start } from './harness';

const TABS = ['Overview', 'Access', 'Data', 'Secrets', 'Timers', 'Logs', 'Settings'];
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
    expect(appSearch({ tab: 'secrets', env: 'preview' })).toEqual({ tab: 'secrets', env: 'preview' });
    expect(appSearch({ tab: 'logs' })).toEqual({ tab: 'logs' });
  });

  it('drops a tab or an environment it does not know', () => {
    expect(appSearch({ tab: 'billing', env: 'staging' })).toEqual({});
    expect(appSearch({ tab: 7, env: ['preview'] })).toEqual({});
    expect(appSearch({ tab: 'timers', env: 'PREVIEW', other: 'x' })).toEqual({ tab: 'timers' });
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
      `/apps/${APP_ID}?tab=access`,
      `/apps/${APP_ID}?tab=data`,
      `/apps/${APP_ID}?tab=secrets`,
      `/apps/${APP_ID}?tab=timers`,
      `/apps/${APP_ID}?tab=logs`,
      `/apps/${APP_ID}?tab=settings`,
    ]);
  });

  it('opens the tab and the environment the address names', async () => {
    const { api } = start(
      `/apps/${APP_ID}?tab=secrets&env=preview`,
      page({ [`GET ${PREVIEW_SECRETS}`]: () => json(200, { environment_id: PREVIEW, items: [] }) }),
      signedIn(),
    );
    const nav = await tabs();
    expect(current(nav)).toEqual(['Secrets']);
    const envs = screen.getByRole('navigation', { name: 'Environment' });
    expect(within(envs).getAllByRole('link').map((a) => a.textContent)).toEqual(['Production', 'Preview']);
    expect(current(envs)).toEqual(['Preview']);
    expect(await screen.findByRole('region', { name: 'Secrets' })).toBeTruthy();
    await waitFor(() => expect(api.of('GET', PREVIEW_SECRETS)).toHaveLength(1));
    expect(api.of('GET', PROD_SECRETS)).toHaveLength(0);
    expect(screen.queryByRole('region', { name: 'Production prod' })).toBeNull();
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' });
    await waitFor(() => expect(crumbs.textContent).toBe('AppsexpensesSecrets'));
  });

  it('shows the overview for a tab and an environment it does not know', async () => {
    start(`/apps/${APP_ID}?tab=billing&env=staging`, page(), signedIn());
    const nav = await tabs();
    expect(current(nav)).toEqual(['Overview']);
    expect(await envPanel('Production')).toBeTruthy();
    expect(within(nav).getByRole('link', { name: 'Secrets' }).getAttribute('href')).toBe(`/apps/${APP_ID}?tab=secrets`);
  });

  it('holds the tab in the address, and the environment across tabs', async () => {
    const { router } = start(`/apps/${APP_ID}`, page(), signedIn());
    const nav = await tabs();
    await openTab('Timers');
    expect(router.state.location.search).toEqual({ tab: 'timers' });
    expect(current(nav)).toEqual(['Timers']);
    expect(current(screen.getByRole('navigation', { name: 'Environment' }))).toEqual(['Production']);

    await chooseEnv('Preview');
    expect(router.state.location.search).toEqual({ tab: 'timers', env: 'preview' });
    await openTab('Logs');
    expect(router.state.location.search).toEqual({ tab: 'logs', env: 'preview' });
    expect(current(screen.getByRole('navigation', { name: 'Environment' }))).toEqual(['Preview']);
    expect(within(nav).getByRole('link', { name: 'Access' }).getAttribute('href')).toBe(
      `/apps/${APP_ID}?tab=access&env=preview`,
    );

    await chooseEnv('Production');
    expect(router.state.location.search).toEqual({ tab: 'logs' });
    await openTab('Settings');
    expect(router.state.location.search).toEqual({ tab: 'settings' });
    expect(screen.queryByRole('navigation', { name: 'Environment' })).toBeNull();
    expect(await screen.findByRole('region', { name: 'Repository' })).toBeTruthy();

    await openTab('Overview');
    expect(router.state.location.search).toEqual({});
    expect(await envPanel('Preview')).toBeTruthy();
  });

  it('steps back through the tabs with the back button', async () => {
    const { router } = start(`/apps/${APP_ID}`, page(), signedIn());
    const nav = await tabs();
    await openTab('Access');
    await openTab('Data');
    expect(current(nav)).toEqual(['Data']);
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
              { id: 'gnt_000000000000000000u1', role: 'builder', subject_kind: 'user', subject_id: 'usr_dddddddddddddddddddd' },
              { id: 'gnt_000000000000000000u2', role: 'user', subject_kind: 'user', subject_id: 'usr_eeeeeeeeeeeeeeeeeeee' },
              { id: 'gnt_000000000000000000g1', role: 'user', subject_kind: 'group', subject_id: 'grp_ffffffffffffffffffff' },
            ],
          }),
      }),
      signedIn(),
    );
    const prod = await envPanel('Production');
    expect(await within(prod).findByText('Everyone in the organisation, 2 people, 1 group')).toBeTruthy();
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
    expect(await within(prod).findByText('Everyone in the organisation, 1 group')).toBeTruthy();
  });
});
