/** One app with both environments, and the routes its page reads, for the app page's tests. */
import { act, fireEvent, screen, within } from '@testing-library/react';
import { type Handler, json } from './fakeApi';

export const OWNER = 'usr_cccccccccccccccccccc';
export const APP_ID = 'app_aaaaaaaaaaaaaaaaaaaa';
export const PROD = 'env_prod0000000000000000';
export const PREVIEW = 'env_preview0000000000000';
export const APP_PATH = `/v1/apps/${APP_ID}`;
export const PROD_GRANTS = `${APP_PATH}/environments/${PROD}/grants`;
export const PREVIEW_GRANTS = `${APP_PATH}/environments/${PREVIEW}/grants`;

/** The first render in a worker can be slow while modules load under parallel test files. */
export const FIRST_RENDER_MS = 5000;

export type Status = 'active' | 'disabled' | 'quarantined' | 'deleting';

export function app(status: Status = 'active') {
  return {
    id: APP_ID,
    slug: 'expenses',
    owner_user_id: OWNER,
    owner_name: 'Olive Owner',
    owner_email: 'olive@example.com',
    status,
    created_at: '2026-09-28T10:00:00Z',
    environments: [
      { id: PREVIEW, name: 'preview', config_version: 1, grants_version: 0, current_deployment_id: null, url: null },
      { id: PROD, name: 'prod', config_version: 2, grants_version: 3, current_deployment_id: null, url: null },
    ],
  };
}

export const ORG_USER = {
  id: 'gnt_000000000000000000o1',
  role: 'user',
  subject_kind: 'org',
  subject_id: null,
  subject_name: null,
  subject_email: null,
} as const;

export function whoami(role: 'admin' | 'member' | null): Handler {
  return () =>
    json(200, {
      org_id: 'org_ffffffffffffffffffff',
      subject: OWNER,
      kind: 'user',
      credential_id: 'c',
      is_agent: false,
      client_id: null,
      role,
      org_name: 'Acme',
      name: 'Olive Owner',
      email: 'olive@example.com',
    });
}

/** The app page's own reads, with `extra` added or replacing them. */
export function page(extra: Record<string, Handler | Handler[]> = {}, status: Status = 'active') {
  return {
    [`GET ${APP_PATH}`]: () => json(200, app(status)),
    [`GET ${PROD_GRANTS}`]: () => json(200, { environment_id: PROD, grants_version: 3, grants: [ORG_USER] }),
    [`GET ${PREVIEW_GRANTS}`]: () => json(200, { environment_id: PREVIEW, grants_version: 0, grants: [] }),
    ...extra,
  };
}

/** One environment's card on the app page's overview. */
export async function envPanel(name: 'Production' | 'Preview'): Promise<HTMLElement> {
  const label = name === 'Production' ? 'Production prod' : 'Preview preview';
  return screen.findByRole('region', { name: label }, { timeout: FIRST_RENDER_MS });
}

/** The tab each of the app page's sections is on. */
const TAB_OF: Readonly<Record<string, string>> = {
  'Who has access': 'Access',
  'Why can this person open it?': 'Access',
  'Data connections': 'Access',
  Secrets: 'Settings',
  Timers: 'Settings',
  Logs: 'Logs',
  Repository: 'Settings',
  'Admin actions': 'Settings',
};

/** Follows one of the app page's tabs, as a click on it does. */
export async function openTab(name: string): Promise<void> {
  const tabs = await screen.findByRole('navigation', { name: 'App sections' }, { timeout: FIRST_RENDER_MS });
  await act(async () => {
    fireEvent.click(within(tabs).getByRole('link', { name }));
  });
}

/** Turns the Production / Preview switch of a per-environment tab. */
export async function chooseEnv(name: 'Production' | 'Preview'): Promise<void> {
  const envs = await screen.findByRole('navigation', { name: 'Environment' });
  await act(async () => {
    fireEvent.click(within(envs).getByRole('link', { name }));
  });
}

/**
 * Opens one of the app page's sections, such as "Timers", and returns it. Given an overview
 * card it goes to the section's tab for that card's environment; given anything else it opens
 * the collapsed section of that name inside it, such as a connection's "Columns".
 */
export async function openSection(panel: HTMLElement, summary: string): Promise<HTMLElement> {
  const tab = TAB_OF[summary];
  if (tab && panel.dataset.env) {
    const preview = panel.dataset.env === 'preview';
    await openTab(tab);
    if (preview) await chooseEnv('Preview');
    return screen.findByRole('region', { name: summary });
  }
  const details = within(panel).getByText(summary).closest('details');
  if (!details) throw new Error(`no section ${summary}`);
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
  });
  return details;
}
