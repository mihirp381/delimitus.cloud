import { type ApiClient, must } from './client';
import type { components } from './schema';

export type AuditEvent = components['schemas']['AuditEvent'];
export type AuditPage = components['schemas']['AuditPage'];
export type AuditAction = components['schemas']['AuditAction'];
export type ActorKind = components['schemas']['ActorKind'];
export type ExportFormat = 'csv' | 'jsonl';

// Records keyed by the generated unions: a value the API adds or removes fails the typecheck.
const ACTIONS: Readonly<Record<AuditAction, true>> = {
  'org.created': true,
  'org.updated': true,
  'user.created': true,
  'user.updated': true,
  'user.deactivated': true,
  'user.reactivated': true,
  'group.synced': true,
  'app.created': true,
  'app.owner_transferred': true,
  'app.disabled': true,
  'app.quarantined': true,
  'app.enabled': true,
  'app.deleted': true,
  'login.succeeded': true,
  'login.failed': true,
  'token.issued': true,
  'token.revoked': true,
  'secret.bound': true,
  'secret.rotated': true,
  'secret.removed': true,
  'grant.added': true,
  'grant.removed': true,
  'bundle.stored': true,
  'build.started': true,
  'build.failed': true,
  'release.created': true,
  'deploy.started': true,
  'deploy.finished': true,
  'deploy.failed': true,
  'rollback.started': true,
  'rollback.finished': true,
  'rollback.failed': true,
  'kill_switch.step': true,
  'approval.requested': true,
  'approval.decided': true,
  'approval.cancelled': true,
  'schedule.created': true,
  'schedule.updated': true,
  'schedule.paused': true,
  'schedule.resumed': true,
  'schedule.deleted': true,
  'schedule.run_requested': true,
  'connection.created': true,
  'connection.removed': true,
  'connection.updated': true,
  'connection.granted': true,
  'connection.revoked': true,
  'connection.ceiling_lowered': true,
  'connection.flagged': true,
  'operator.access': true,
  'audit.exported': true,
  'audit.reanchored': true,
  'directory.connected': true,
  'directory.frozen': true,
  'identity.linked': true,
  'cell.resource_requested': true,
  'cell.resource_ready': true,
  'cell.resource_failed': true,
  'github.installation_bound': true,
  'repo.connected': true,
  'repo.disconnected': true,
};
const KINDS: Readonly<Record<ActorKind, true>> = {
  user: true,
  workload: true,
  schedule: true,
  operator: true,
  integration: true,
};

export const AUDIT_ACTIONS = Object.keys(ACTIONS) as readonly AuditAction[];
export const ACTOR_KINDS = Object.keys(KINDS) as readonly ActorKind[];

/** The search filters, as the audit page keeps them in its URL. Absent means "any". */
export interface AuditFilters {
  readonly since?: string;
  readonly until?: string;
  readonly action?: AuditAction;
  readonly actor_kind?: ActorKind;
  readonly actor_id?: string;
  readonly target_kind?: string;
  readonly target_id?: string;
}

const REFS = ['actor_id', 'target_kind', 'target_id'] as const;

function text(value: unknown): string | undefined {
  // The router parses `?id=123` as a number; ids are text.
  const s = typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
  return s.length >= 1 && s.length <= 200 ? s : undefined;
}

function instant(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value.trim()) return undefined;
  const at = new Date(value);
  return Number.isNaN(at.getTime()) ? undefined : at.toISOString();
}

/**
 * Filters from a URL's search, keeping only values the API accepts: a known action and actor
 * kind, 1 to 200 characters for ids, and instants as RFC 3339 in UTC (the API refuses a time
 * without an offset). Anything else is dropped rather than sent to be refused.
 */
export function auditFilters(search: Record<string, unknown>): AuditFilters {
  const out: Record<string, string> = {};
  const since = instant(search.since);
  const until = instant(search.until);
  if (since) out.since = since;
  if (until) out.until = until;
  if (typeof search.action === 'string' && Object.hasOwn(ACTIONS, search.action)) {
    out.action = search.action;
  }
  if (typeof search.actor_kind === 'string' && Object.hasOwn(KINDS, search.actor_kind)) {
    out.actor_kind = search.actor_kind;
  }
  for (const key of REFS) {
    const value = text(search[key]);
    if (value) out[key] = value;
  }
  return out as AuditFilters;
}

/** The name the API gives the download, or `audit.<format>` when it gives none usable. */
export function exportFileName(disposition: string | null, format: ExportFormat): string {
  const name = /filename="([^"]+)"/.exec(disposition ?? '')?.[1];
  return name && /^[\w.-]+$/.test(name) ? name : `audit.${format}`;
}

/**
 * Every event matching `filters` as one file. The API streams it and records the export as
 * `audit.exported`; the console holds the file in memory until the browser saves it.
 */
export async function exportAudit(
  api: ApiClient,
  filters: AuditFilters,
  format: ExportFormat,
): Promise<{ readonly blob: Blob; readonly fileName: string }> {
  const result = await api.GET('/v1/audit/export', {
    params: { query: { ...filters, format } },
    parseAs: 'blob',
  });
  const blob = must(result);
  return { blob, fileName: exportFileName(result.response.headers.get('Content-Disposition'), format) };
}
