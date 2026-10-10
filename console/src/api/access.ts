import { type ApiClient, must } from './client';
import type { components } from './schema';

export type AccessExplained = components['schemas']['AccessExplained'];
export type ExplainedGrant = components['schemas']['ExplainedGrant'];
export type AccessReason = AccessExplained['reason'];

/** Why `userId` can or cannot open the environment, from the org's current sharing rules. */
export async function explainAccess(
  api: ApiClient,
  appId: string,
  environmentId: string,
  userId: string,
): Promise<AccessExplained> {
  return must(
    await api.GET('/v1/apps/{app_id}/environments/{environment_id}/access', {
      params: { path: { app_id: appId, environment_id: environmentId }, query: { user_id: userId } },
    }),
  );
}

/**
 * The answer in one sentence, as `ssc access explain` says it. Keyed by the generated union: a
 * reason the API adds fails the typecheck until it has words here.
 */
const SENTENCE: Readonly<Record<AccessReason, (r: AccessExplained, who: string, where: string, env: string) => string>> = {
  granted: (r, who, where) => `${who} can open ${where} as ${r.role ?? 'user'}, through the grants below.`,
  below_floor: (r, who, where, env) =>
    `${who} cannot open ${where}: ${env} takes ${r.floor} grants or higher, and the grants below are lower.`,
  no_grant: (_r, who, where) =>
    `${who} cannot open ${where}: no grant names them, a group of theirs or everyone in the organisation.`,
  app_not_active: (_r, who, where) => `${who} cannot open ${where}: the app is stopped, so no one can.`,
  user_not_active: (_r, who, where) => `${who} cannot open ${where}: they are deactivated.`,
  unknown_environment: (_r, who, where) =>
    `${who} cannot open ${where}: the sharing rules do not know this environment yet.`,
  no_view: (_r, who, where) =>
    `${who} cannot open ${where}: the organisation has no sharing rules to check yet.`,
};

/** `who` is the person as shown, `where` such as "production of expenses", `env` such as "production". */
export function accessSentence(r: AccessExplained, who: string, where: string, env: string): string {
  return SENTENCE[r.reason](r, who, where, env);
}

/** Who a deciding grant names, in words. */
export function grantSubject(g: ExplainedGrant): string {
  if (g.subject_kind === 'org') return 'Everyone in the organisation';
  if (g.subject_kind === 'group') return `Group ${g.group_name ?? g.subject_id ?? ''}`;
  return `User ${g.subject_id ?? ''}`;
}
