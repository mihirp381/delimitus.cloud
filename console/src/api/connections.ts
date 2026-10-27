import { type ApiClient, must } from './client';
import { findGroups, findPeople, GROUP_ID_PATTERN, type Match } from './directory';
import { type Approval, type Grant, type GrantInput, readGrants, toInput } from './grants';
import type { components } from './schema';

export type Connection = components['schemas']['ConnectionOut'];
export type ConnectionInput = components['schemas']['ConnectionIn'];
export type ConnectionChange = components['schemas']['ConnectionPatch'];
export type Ceiling = components['schemas']['CeilingDoc'];
export type Subject = components['schemas']['SubjectDoc'];
export type Limits = components['schemas']['SnapshotLimits'];
export type PreviewAccess = components['schemas']['PreviewAccessDoc'];
export type PreviewWho = PreviewAccess['who'];
export type Classification = Connection['classification'];
export type Kind = Connection['kind'];
export type EnvironmentConnection = components['schemas']['EnvironmentConnectionOut'];
export type EnvironmentConnections = components['schemas']['EnvironmentConnectionsOut'];
/** The tables and columns an environment sees through a connection, from the cell's data gateway (GA-5.8). */
export type ConnectionSchema = components['schemas']['ConnectionSchemaOut'];

export const CLASSIFICATIONS: readonly Classification[] = ['internal', 'confidential', 'restricted'];

export const KIND_TITLE: Readonly<Record<Kind, string>> = {
  postgres: 'PostgreSQL',
  mysql: 'MySQL',
  sqlserver: 'SQL Server',
  bigquery: 'BigQuery',
  snowflake: 'Snowflake',
  gsheets: 'Google Sheets',
  gcs: 'Google Cloud Storage',
  s3: 'Amazon S3',
  airtable: 'Airtable',
  rest: 'REST (GET)',
};

/**
 * The kinds a connector exists for (`ssc_contracts.connections.AVAILABLE`). The API refuses any
 * other kind with CONNECTOR_UNAVAILABLE, so the picker offers only these.
 */
export const AVAILABLE_KINDS: readonly Kind[] = ['postgres', 'mysql', 'sqlserver', 'rest', 'gsheets', 's3', 'gcs', 'bigquery', 'airtable', 'snowflake'];

/** The kinds whose address is a host, a port and a database, sent as the three top-level members. */
export const SQL_KINDS: readonly Kind[] = ['postgres', 'mysql', 'sqlserver'];

export const DEFAULT_PORT: Readonly<Partial<Record<Kind, string>>> = { postgres: '5432', mysql: '3306', sqlserver: '1433' };

export interface AddressField {
  readonly key: string;
  readonly label: string;
  readonly optional?: boolean;
  readonly numeric?: boolean;
}

const SQL_ADDRESS: readonly AddressField[] = [
  { key: 'host', label: 'Host' },
  { key: 'port', label: 'Port', numeric: true },
  { key: 'database', label: 'Database' },
];

/** What an admin types for each kind: where the source is, never how to log in. */
export const ADDRESS_FIELDS: Readonly<Record<Kind, readonly AddressField[]>> = {
  postgres: SQL_ADDRESS,
  mysql: SQL_ADDRESS,
  sqlserver: SQL_ADDRESS,
  bigquery: [
    { key: 'project', label: 'Project id' },
    { key: 'dataset', label: 'Dataset' },
    { key: 'location', label: 'Location (US when left out)', optional: true },
  ],
  snowflake: [
    { key: 'account', label: 'Account identifier (org-account)' },
    { key: 'database', label: 'Database' },
    { key: 'schema', label: 'Schema (PUBLIC when left out)', optional: true },
    { key: 'warehouse', label: 'Warehouse' },
    { key: 'role', label: 'Role (the user\'s default when left out)', optional: true },
  ],
  gsheets: [
    { key: 'spreadsheet_id', label: 'Spreadsheet id (from its URL)' },
    { key: 'sheet', label: 'Sheet tab (every tab when left out)', optional: true },
  ],
  gcs: [
    { key: 'bucket', label: 'Bucket' },
    { key: 'prefix', label: 'Prefix apps may read (the whole bucket when left out)', optional: true },
  ],
  s3: [
    { key: 'bucket', label: 'Bucket' },
    { key: 'region', label: 'Region (eu-west-1)' },
    { key: 'prefix', label: 'Prefix apps may read (the whole bucket when left out)', optional: true },
  ],
  airtable: [
    { key: 'base_id', label: 'Base id (app…)' },
    { key: 'table', label: 'Table (every table when left out)', optional: true },
  ],
  rest: [{ key: 'base_url', label: 'Base URL (https)' }],
};

/** The default address for a kind: the engine's port for the SQL kinds, otherwise empty. */
export function emptyAddress(kind: Kind): Record<string, string> {
  const port = DEFAULT_PORT[kind];
  return port === undefined ? {} : { port };
}

/** True when every required address field of the kind has a value. */
export function addressComplete(kind: Kind, typed: Readonly<Record<string, string>>): boolean {
  return ADDRESS_FIELDS[kind].every((f) => f.optional || (typed[f.key] ?? '').trim() !== '');
}

/**
 * The typed address as the API takes it: numbers for numeric fields, optional fields left out
 * when empty. Throws an Error naming a numeric field that is not a whole number.
 */
export function parseAddress(kind: Kind, typed: Readonly<Record<string, string>>): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const f of ADDRESS_FIELDS[kind]) {
    const value = (typed[f.key] ?? '').trim();
    if (value === '') continue;
    if (f.numeric) {
      if (!/^\d+$/.test(value)) throw new Error(`${f.label} must be a whole number.`);
      out[f.key] = Number(value);
    } else {
      out[f.key] = value;
    }
  }
  return out;
}

/** `confidential` and `restricted` connections need a ceiling (CEILING_REQUIRED without one). */
export function needsCeiling(classification: Classification): boolean {
  return classification !== 'internal';
}

export const PREVIEW_WHO_TITLE: Readonly<Record<PreviewWho, string>> = {
  approval: 'Approval required',
  builders: 'Any builder of the app',
  group: 'Members of a group',
};

export function defaultPreviewWho(classification: Classification): PreviewWho {
  return classification === 'internal' ? 'builders' : 'approval';
}

/** Throws an Error when a group rule's id is not a grp_ id. */
export function previewAccessOf(who: PreviewWho, group: string): PreviewAccess {
  if (who !== 'group') return { who };
  const id = group.trim();
  if (!GROUP_ID_PATTERN.test(id)) throw new Error('The preview access group must be a grp_ id.');
  return { who, group_id: id };
}

export const LIMIT_TITLE: Readonly<Record<keyof Limits, string>> = {
  max_rows: 'Rows per query',
  max_bytes: 'Bytes per query',
  timeout_ms: 'Query timeout (ms)',
  concurrency: 'Queries at once',
  daily_rows: 'Rows a day',
  daily_bytes: 'Bytes a day',
};

/** The limits each gateway instance counts for itself (decision 034): a grant can reach up to ten times them. */
export const ADVISORY_LIMITS: readonly (keyof Limits)[] = ['concurrency', 'daily_rows', 'daily_bytes'];

export const ADVISORY_NOTE =
  'Rows per query, bytes per query and the timeout are exact. Queries at once, rows a day and bytes a day are advisory: each gateway instance counts its own, and up to ten run, so a busy app can reach up to ten times them.';

export const SCHEMAS_NOTE =
  "Recorded for the setup script and this console, and not enforced by the data gateway: apps read whatever the connection's database role may read, so the setup script's grants are what bound it.";

export const LIMIT_KEYS = Object.keys(LIMIT_TITLE) as readonly (keyof Limits)[];

/** The limits that are set, one phrase each: "Rows per query 1000", "Queries at once 4". Empty when none is. */
export function limitPhrases(limits: Limits): string[] {
  const set = LIMIT_KEYS.filter((k) => limits[k] !== undefined && limits[k] !== null);
  return set.map((k) => `${LIMIT_TITLE[k]} ${String(limits[k])}`);
}

export const SUBJECT_PATTERN = /^(usr|grp)_[a-z0-9]{20}$/;

export function subjectMatches(c: Connection): Match[] {
  return (c.ceiling.subjects ?? []).map((s) => ({ id: s.id, label: c.ceiling_subject_names[s.id] ?? s.id }));
}

export function subjectsOf(picked: readonly Match[]): Subject[] {
  return picked
    .map((m): Subject => ({ kind: m.id.startsWith('usr_') ? 'user' : 'group', id: m.id }))
    .sort((a, b) => (a.kind === b.kind ? (a.id < b.id ? -1 : 1) : a.kind < b.kind ? -1 : 1));
}

export function ceilingText(c: Connection): string {
  if (c.ceiling.audience === 'org') return 'Anyone in the organisation';
  return subjectMatches(c)
    .map((m) => m.label)
    .join(', ');
}

export function findSubjects(api: ApiClient, text: string): Promise<readonly Match[]> {
  return text.includes('@') ? findPeople(api, text) : findGroups(api, text);
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
 * (`exceed_ceiling`, decided by the connection's owner or an org admin). The request names
 * `grants` when given: the desired set a sharing change was refused for, which the server's
 * requirement is keyed on. Without it, the environment's grants as they are now, which an attach
 * is checked against.
 */
export async function askCeilingApproval(
  api: ApiClient,
  target: EnvironmentTarget,
  connection: string,
  grants?: readonly (Grant | GrantInput)[],
): Promise<Approval> {
  const named = grants ?? (await readGrants(api, target)).grants.grants;
  return must(
    await api.POST('/v1/approvals', {
      body: {
        environment_id: target.environmentId,
        kind: 'exceed_ceiling',
        payload: { connection, grants: named.map(toInput) },
      },
    }),
  );
}
