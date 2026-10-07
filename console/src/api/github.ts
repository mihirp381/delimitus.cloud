import { type ApiClient, must } from './client';
import { ApiProblem } from './problem';
import type { components } from './schema';

export type RepoLink = components['schemas']['RepoLinkOut'];
export type RepoLinkInput = components['schemas']['RepoLinkIn'];
export type RequiredCheck = components['schemas']['RequiredCheckIn'];

export const REPOSITORY_PATTERN = /^[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}$/;
const WORKFLOW_PATTERN = /^\.github\/workflows\/[A-Za-z0-9._/-]+\.ya?ml$/;

/** The connected repository, or null when none is (the API answers NOT_FOUND). */
export async function readRepository(
  api: ApiClient,
  appId: string,
  signal?: AbortSignal,
): Promise<RepoLink | null> {
  try {
    return must(await api.GET('/v1/apps/{app_id}/github', { params: { path: { app_id: appId } }, signal }));
  } catch (e) {
    if (e instanceof ApiProblem && e.status === 404 && e.code === 'NOT_FOUND') return null;
    throw e;
  }
}

/**
 * Connects the app to a repository, or changes its branch or required checks. A repository the
 * org's GitHub App installation cannot reach is REPOSITORY_NOT_INSTALLED, whose detail says how
 * to install it.
 */
export async function connectRepository(
  api: ApiClient,
  appId: string,
  input: RepoLinkInput,
): Promise<RepoLink> {
  return must(
    await api.PUT('/v1/apps/{app_id}/github', { params: { path: { app_id: appId } }, body: input }),
  );
}

export async function disconnectRepository(api: ApiClient, appId: string): Promise<void> {
  await api.DELETE('/v1/apps/{app_id}/github', { params: { path: { app_id: appId } } });
}

/**
 * Required checks typed one per line as `<workflow file> <check name>`, for example
 * `.github/workflows/ci.yml test`. Throws an Error naming the first line that is not.
 */
export function parseChecks(text: string): RequiredCheck[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const space = line.search(/\s/);
      const workflow = space < 0 ? line : line.slice(0, space);
      const name = space < 0 ? '' : line.slice(space).trim();
      if (!WORKFLOW_PATTERN.test(workflow) || !name) {
        throw new Error(`"${line}" is not a workflow file under .github/workflows and a check name.`);
      }
      return { workflow, name };
    });
}

export function checksText(checks: readonly RequiredCheck[]): string {
  return checks.map((c) => `${c.workflow} ${c.name}`).join('\n');
}
