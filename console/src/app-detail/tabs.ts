import type { EnvironmentOut } from '../api/lifecycle';

export const APP_TABS = ['overview', 'logs', 'access', 'settings'] as const;
export type AppTab = (typeof APP_TABS)[number];

export const APP_TAB_TITLE: Readonly<Record<AppTab, string>> = {
  overview: 'Overview',
  logs: 'Logs',
  access: 'Access',
  settings: 'Settings',
};

/** Tabs from before the merge, and where each now lives. */
export const MOVED_TABS = {
  data: { tab: 'access', hash: 'data-connections' },
  secrets: { tab: 'settings', hash: 'secrets' },
  timers: { tab: 'settings', hash: 'timers' },
} as const satisfies Record<string, { readonly tab: AppTab; readonly hash: string }>;
export type MovedTab = keyof typeof MOVED_TABS;

export function movedTab(value: unknown): MovedTab | null {
  return typeof value === 'string' && Object.hasOwn(MOVED_TABS, value) ? (value as MovedTab) : null;
}

export type EnvName = EnvironmentOut['name'];

export function appTab(value: unknown): AppTab {
  const moved = movedTab(value);
  if (moved) return MOVED_TABS[moved].tab;
  return typeof value === 'string' && (APP_TABS as readonly string[]).includes(value) ? (value as AppTab) : 'overview';
}

export function appEnv(value: unknown): EnvName {
  return value === 'preview' ? 'preview' : 'prod';
}

export interface AppSearch {
  readonly tab?: AppTab | MovedTab;
  readonly env?: EnvName;
}

/** Unknown values are dropped and the defaults (overview, production) left out of the address. */
export function appSearch(search: Record<string, unknown>): AppSearch {
  const tab = appTab(search.tab);
  const env = appEnv(search.env);
  return {
    ...(tab === 'overview' ? {} : { tab }),
    ...(env === 'prod' ? {} : { env }),
  };
}

/** As `appSearch`, but an old tab is kept so the route can redirect it with its section's hash. */
export function validateAppSearch(search: Record<string, unknown>): AppSearch {
  const moved = movedTab(search.tab);
  return moved ? { ...appSearch(search), tab: moved } : appSearch(search);
}
