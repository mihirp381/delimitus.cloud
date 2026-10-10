import type { EnvironmentOut } from '../api/lifecycle';

/** The app page's tabs, in order. The first is where an address without `tab` lands. */
export const APP_TABS = ['overview', 'access', 'data', 'secrets', 'timers', 'logs', 'settings'] as const;
export type AppTab = (typeof APP_TABS)[number];

export const APP_TAB_TITLE: Readonly<Record<AppTab, string>> = {
  overview: 'Overview',
  access: 'Access',
  data: 'Data',
  secrets: 'Secrets',
  timers: 'Timers',
  logs: 'Logs',
  settings: 'Settings',
};

/** The tabs that show one environment at a time, chosen with the Production / Preview switch. */
export const ENV_TABS: ReadonlySet<AppTab> = new Set(['access', 'data', 'secrets', 'timers', 'logs']);

export type EnvName = EnvironmentOut['name'];
export const ENV_NAMES: readonly EnvName[] = ['prod', 'preview'];

/** The tab an address names; anything else, or nothing, is the overview. */
export function appTab(value: unknown): AppTab {
  return typeof value === 'string' && (APP_TABS as readonly string[]).includes(value) ? (value as AppTab) : 'overview';
}

/** The environment an address names; anything else, or nothing, is production. */
export function appEnv(value: unknown): EnvName {
  return value === 'preview' ? 'preview' : 'prod';
}

export interface AppSearch {
  readonly tab?: AppTab;
  readonly env?: EnvName;
}

/**
 * The app page's search, held to what it knows: an unknown tab or environment is dropped, and
 * the defaults (overview, production) are left out so the plain address stays the plain address.
 */
export function appSearch(search: Record<string, unknown>): AppSearch {
  const tab = appTab(search.tab);
  const env = appEnv(search.env);
  return {
    ...(tab === 'overview' ? {} : { tab }),
    ...(env === 'prod' ? {} : { env }),
  };
}
