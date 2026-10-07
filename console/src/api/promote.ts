import { type ApiClient, must } from './client';
import type { components } from './schema';

export type Build = components['schemas']['BuildOut'];
export type BuildAccepted = components['schemas']['BuildAccepted'];
export type CapabilityChange = components['schemas']['CapabilityChange'];

/**
 * Builds for production the source of the release live in preview. `previewReleaseId` is the
 * release the person saw there: PRECONDITION_STALE when preview has moved on since. The build
 * makes a production release; `deployRelease` puts it live.
 */
export async function promote(
  api: ApiClient,
  appId: string,
  previewReleaseId: string,
): Promise<BuildAccepted> {
  return must(
    await api.POST('/v1/apps/{app_id}/promote', {
      params: { path: { app_id: appId } },
      body: { preview_release_id: previewReleaseId },
    }),
  );
}

/** Deploys a release forward to the environment; the operation id to poll. */
export async function deployRelease(
  api: ApiClient,
  appId: string,
  environmentId: string,
  releaseId: string,
): Promise<string> {
  const body = must(
    await api.POST('/v1/apps/{app_id}/environments/{environment_id}/deployments', {
      params: { path: { app_id: appId, environment_id: environmentId } },
      body: { release_id: releaseId, kind: 'deploy', confirm: false },
    }),
  );
  return body.operation_id;
}

export function buildFinished(build: Build | undefined): boolean {
  return build !== undefined && (build.state === 'succeeded' || build.state === 'failed');
}
