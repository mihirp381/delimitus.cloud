import type { Tone } from '../components/Badge';
import type { components } from './schema';

export type AppSummary = components['schemas']['AppSummary'];
export type InventoryApp = components['schemas']['InventoryApp'];
export type InventoryEnvironment = components['schemas']['InventoryEnvironment'];
export type AppStatus = AppSummary['status'];
export type EnvName = InventoryEnvironment['name'];

// Keyed by the generated unions: a value the API adds or removes fails the typecheck.
const STATUSES: Readonly<Record<AppStatus, true>> = { active: true, disabled: true, quarantined: true };
export const APP_STATUSES = Object.keys(STATUSES) as readonly AppStatus[];

export const DEPLOY_TONE: Readonly<Record<NonNullable<InventoryEnvironment['last_deploy']>['state'], Tone>> = {
  pending: 'info',
  running: 'info',
  healthy: 'success',
  failed: 'danger',
  superseded: 'neutral',
};

function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Who an environment is shared with, in a line: "Everyone in the organisation, 2 people, 1 group". */
export function sharingSummary(s: InventoryEnvironment['sharing']): string {
  const parts = [
    s.org_wide ? 'Everyone in the organisation' : '',
    s.users > 0 ? count(s.users, 'person', 'people') : '',
    s.groups > 0 ? count(s.groups, 'group', 'groups') : '',
  ].filter(Boolean);
  return parts.length ? parts.join(', ') : 'Nobody yet';
}

export interface AppFilter {
  /** Part of a slug, or of an owner's name or id. */
  readonly text: string;
  readonly status: AppStatus | '';
}

interface Filterable {
  readonly slug: string;
  readonly status: AppStatus;
  /** What an owner can be found by: the id, and the name where the list carries it. */
  readonly owner: readonly string[];
}

export function matchesFilter(app: Filterable, filter: AppFilter): boolean {
  if (filter.status && app.status !== filter.status) return false;
  const f = filter.text.trim().toLowerCase();
  return !f || app.slug.includes(f) || app.owner.some((o) => o.toLowerCase().includes(f));
}
