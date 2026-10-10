import { type ApiClient, must } from './client';
import type { components } from './schema';

export type LogSource = components['schemas']['LogSource'];
export type LogLine = components['schemas']['LogLineOut'];
export type LogPage = components['schemas']['LogPageOut'];

// Keyed by the generated union: a source the API adds or removes fails the typecheck.
export const SOURCE_TITLE: Readonly<Record<LogSource, string>> = {
  app: 'App: what it printed and its requests',
  build: 'Builds: its last builds',
  deploy: 'Deployments',
};
export const LOG_SOURCES = Object.keys(SOURCE_TITLE) as readonly LogSource[];

/** How many of the newest lines the first read asks for. */
export const FIRST_LINES = 100;
/** Seconds the API holds a follow request open for a new line. */
export const FOLLOW_WAIT_S = 10;
/** The most lines kept on the page; older ones are dropped. */
export const MAX_LINES = 1000;
/** The least time between two follow requests, for an answer that comes back empty at once. */
export const MIN_FOLLOW_GAP_MS = 1000;
/** How long to wait before reading again when a source had no line, and so no cursor to follow from. */
export const EMPTY_AGAIN_MS = 5000;
/** The longest a `Retry-After` is honoured for, and the wait when a refusal names none. */
export const MAX_RETRY_AFTER_S = 60;

const PATH = '/v1/apps/{app_id}/environments/{environment_id}/logs' as const;

interface Where {
  readonly appId: string;
  readonly environmentId: string;
  readonly source: LogSource;
}

/** The newest lines of one source, oldest first, and the cursor to follow from. */
export async function newestLogs(api: ApiClient, where: Where, signal: AbortSignal): Promise<LogPage> {
  return must(
    await api.GET(PATH, {
      params: {
        path: { app_id: where.appId, environment_id: where.environmentId },
        query: { source: where.source, limit: FIRST_LINES },
      },
      signal,
    }),
  );
}

/** The lines after `cursor`; the API waits up to `FOLLOW_WAIT_S` for one before answering with none. */
export async function logsAfter(
  api: ApiClient,
  where: Where,
  cursor: string,
  signal: AbortSignal,
): Promise<LogPage> {
  return must(
    await api.GET(PATH, {
      params: {
        path: { app_id: where.appId, environment_id: where.environmentId },
        query: { source: where.source, after: cursor, wait: FOLLOW_WAIT_S },
      },
      signal,
    }),
  );
}

/** `lines` with `more` added, keeping only the newest `MAX_LINES`. */
export function appended<T>(lines: readonly T[], more: readonly T[]): readonly T[] {
  if (more.length === 0) return lines;
  const all = [...lines, ...more];
  return all.length > MAX_LINES ? all.slice(all.length - MAX_LINES) : all;
}

/** Cloud Logging's severities that mean something went wrong, and the ones that warn. */
export function severityTone(severity: string): 'danger' | 'warning' | 'plain' {
  const s = severity.toUpperCase();
  if (s === 'ERROR' || s === 'CRITICAL' || s === 'ALERT' || s === 'EMERGENCY') return 'danger';
  if (s === 'WARNING') return 'warning';
  return 'plain';
}
