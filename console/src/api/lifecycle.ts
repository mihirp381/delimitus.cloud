import { type ApiClient, must } from './client';
import type { components } from './schema';

export type AppOut = components['schemas']['AppOut'];
export type EnvironmentOut = components['schemas']['EnvironmentOut'];
export type KillSwitchMode = components['schemas']['KillSwitchCreate']['mode'];
export type KillSwitchRun = components['schemas']['KillSwitchRun'];
export type Release = components['schemas']['ReleaseOut'];
export type Operation = components['schemas']['OperationOut'];
export type LedgerAhead = components['schemas']['LedgerAhead'];

/** How often a running kill switch or rollback is read again. */
export const POLL_MS = 1000;

/**
 * Pulls the kill switch (admins only). The answer is 202 while the steps run; the run id comes
 * from the body, because a replayed request may not repeat the `Location` header.
 */
export async function pullKillSwitch(
  api: ApiClient,
  appId: string,
  mode: KillSwitchMode,
): Promise<string> {
  const body = must(
    await api.POST('/v1/apps/{app_id}/kill-switch', {
      params: { path: { app_id: appId } },
      body: { mode },
    }),
  );
  return body.run_id;
}

export async function enableApp(api: ApiClient, appId: string): Promise<AppOut> {
  return must(await api.POST('/v1/apps/{app_id}/enable', { params: { path: { app_id: appId } } }));
}

export async function transferOwner(api: ApiClient, appId: string, userId: string): Promise<AppOut> {
  return must(
    await api.PUT('/v1/apps/{app_id}/owner', {
      params: { path: { app_id: appId } },
      body: { user_id: userId },
    }),
  );
}

/**
 * Starts a rollback to `releaseId`; the operation id to poll. Without `confirm`, a release that
 * lacks migrations the environment's database may have run is refused with SCHEMA_AHEAD.
 */
export async function rollBack(
  api: ApiClient,
  appId: string,
  environmentId: string,
  releaseId: string,
  confirm = false,
): Promise<string> {
  const body = must(
    await api.POST('/v1/apps/{app_id}/environments/{environment_id}/deployments', {
      params: { path: { app_id: appId, environment_id: environmentId } },
      body: { release_id: releaseId, kind: 'rollback', confirm },
    }),
  );
  return body.operation_id;
}

/** The migrations the environment's database may have run that `releaseId` lacks, per tool. */
export async function migrationsAhead(
  api: ApiClient,
  appId: string,
  environmentId: string,
  releaseId: string,
): Promise<readonly LedgerAhead[]> {
  const body = must(
    await api.GET('/v1/apps/{app_id}/environments/{environment_id}/migrations-ahead', {
      params: { path: { app_id: appId, environment_id: environmentId }, query: { release_id: releaseId } },
    }),
  );
  return body.ledgers;
}

/**
 * Whether `release` may run in `env`. A release a build made for another environment is refused
 * (RELEASE_ENVIRONMENT_MISMATCH): production runs only releases built for production.
 */
export function runsIn(release: Release, env: EnvironmentOut): boolean {
  return release.built_for_environment_id === null || release.built_for_environment_id === env.id;
}

export function operationFinished(op: Operation | undefined): boolean {
  return op !== undefined && op.state !== 'pending' && op.state !== 'running';
}
