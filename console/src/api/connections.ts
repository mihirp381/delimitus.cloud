import { type ApiClient, must } from './client';
import { type Approval, readGrants, toInput } from './grants';
import type { components } from './schema';

export type Connection = components['schemas']['ConnectionOut'];
export type ConnectionInput = components['schemas']['ConnectionIn'];
export type ConnectionChange = components['schemas']['ConnectionPatch'];
export type Ceiling = components['schemas']['CeilingDoc'];
export type Subject = components['schemas']['SubjectDoc'];
export type Limits = components['schemas']['SnapshotLimits'];
export type Classification = Connection['classification'];
export type EnvironmentConnection = components['schemas']['EnvironmentConnectionOut'];
export type EnvironmentConnections = components['schemas']['EnvironmentConnectionsOut'];

export const CLASSIFICATIONS: readonly Classification[] = ['internal', 'confidential', 'restricted'];

/** `confidential` and `restricted` connections need a ceiling (CEILING_REQUIRED without one). */
export function needsCeiling(classification: Classification): boolean {
  return classification !== 'internal';
}

export const LIMIT_TITLE: Readonly<Record<keyof Limits, string>> = {
  max_rows: 'Rows per query',
  max_bytes: 'Bytes per query',
  timeout_ms: 'Query timeout (ms)',
  concurrency: 'Queries at once',
  daily_rows: 'Rows a day',
  daily_bytes: 'Bytes a day',
};

export const LIMIT_KEYS = Object.keys(LIMIT_TITLE) as readonly (keyof Limits)[];

/** The limits that are set, as "Rows per query 1000, Queries at once 4"; "None" otherwise. */
export function limitsText(limits: Limits): string {
  const set = LIMIT_KEYS.filter((k) => limits[k] !== undefined && limits[k] !== null);
  if (set.length === 0) return 'None';
  return set.map((k) => `${LIMIT_TITLE[k]} ${String(limits[k])}`).join(', ');
}

/** Who may use an app on the connection, in words. */
export function ceilingText(ceiling: Ceiling): string {
  if (ceiling.audience === 'org') return 'Anyone in the organisation';
  const subjects = ceiling.subjects ?? [];
  return `${subjects.length} listed ${subjects.length === 1 ? 'group or person' : 'groups and people'}`;
}

const SUBJECT_PATTERN = /^(usr|grp)_[a-z0-9]{20}$/;

/**
 * The subjects of a ceiling typed one per line as `usr_…` or `grp_…` ids. Throws an Error naming
 * the first line that is neither.
 */
export function parseSubjects(text: string): Subject[] {
  const ids = text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  return [...new Set(ids)].map((id) => {
    if (!SUBJECT_PATTERN.test(id)) throw new Error(`${id} is not a usr_ or grp_ id.`);
    return { kind: id.startsWith('usr_') ? 'user' : 'group', id };
  });
}

export function subjectsText(ceiling: Ceiling): string {
  return (ceiling.subjects ?? []).map((s) => s.id).join('\n');
}

/** Schema names typed with commas or spaces between them. */
export function parseSchemas(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\s,]+/)
        .map((s) => s.trim())
        .filter(Boolean),
    ),
  ];
}

/**
 * Typed limits: an empty box leaves that limit unset. Throws an Error naming a box that holds
 * anything but a whole number of zero or more.
 */
export function parseLimits(typed: Readonly<Partial<Record<keyof Limits, string>>>): Limits {
  const limits: Limits = {};
  for (const key of LIMIT_KEYS) {
    const text = (typed[key] ?? '').trim();
    if (!text) continue;
    if (!/^\d+$/.test(text)) throw new Error(`${LIMIT_TITLE[key]} must be a whole number.`);
    limits[key] = Number(text);
  }
  return limits;
}

export function limitsTyped(limits: Limits): Partial<Record<keyof Limits, string>> {
  const typed: Partial<Record<keyof Limits, string>> = {};
  for (const key of LIMIT_KEYS) {
    const value = limits[key];
    if (value !== undefined && value !== null) typed[key] = String(value);
  }
  return typed;
}

/**
 * Adds a connection (admins only). The address goes to the API once and is never returned, so
 * the caller passes it straight here and keeps no copy: no query key, cache or URL holds it.
 */
export async function createConnection(api: ApiClient, input: ConnectionInput): Promise<Connection> {
  return must(await api.POST('/v1/connections', { body: input }));
}

export async function changeConnection(
  api: ApiClient,
  name: string,
  change: ConnectionChange,
): Promise<Connection> {
  return must(
    await api.PATCH('/v1/connections/{name}', { params: { path: { name } }, body: change }),
  );
}

interface EnvironmentTarget {
  readonly appId: string;
  readonly environmentId: string;
}

/**
 * Lets the environment reach a connection (admins only). While the environment's audience is
 * wider than the connection's ceiling the API answers APPROVAL_REQUIRED; see `askCeilingApproval`.
 */
export async function attachConnection(
  api: ApiClient,
  target: EnvironmentTarget,
  name: string,
): Promise<EnvironmentConnections> {
  return must(
    await api.PUT('/v1/apps/{app_id}/environments/{environment_id}/connections/{name}', {
      params: { path: { app_id: target.appId, environment_id: target.environmentId, name } },
      body: {},
    }),
  );
}

export async function detachConnection(
  api: ApiClient,
  target: EnvironmentTarget,
  name: string,
): Promise<EnvironmentConnections> {
  return must(
    await api.DELETE('/v1/apps/{app_id}/environments/{environment_id}/connections/{name}', {
      params: { path: { app_id: target.appId, environment_id: target.environmentId, name } },
    }),
  );
}

/**
 * Asks for the environment to use `connection` with an audience wider than its ceiling
 * (`exceed_ceiling`, decided by the connection's owner or an org admin). The request names the
 * environment's grants as they are now, which the attach is checked against.
 */
export async function askCeilingApproval(
  api: ApiClient,
  target: EnvironmentTarget,
  connection: string,
): Promise<Approval> {
  const { grants } = await readGrants(api, target);
  return must(
    await api.POST('/v1/approvals', {
      body: {
        environment_id: target.environmentId,
        kind: 'exceed_ceiling',
        payload: { connection, grants: grants.grants.map(toInput) },
      },
    }),
  );
}
