import { useRouteContext } from '@tanstack/react-router';
import { useState } from 'react';
import { etagOf, type Grant, type GrantsUpdate, updateGrants, withoutGrant } from '../api/grants';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';

const PATH = '/v1/apps/{app_id}/environments/{environment_id}/grants' as const;

/** Who a grant names, as a person would say it. */
export function who(g: Grant): string {
  if (g.subject_kind === 'org') return 'Everyone in the organisation';
  return `${g.subject_kind === 'user' ? 'User' : 'Group'} ${g.subject_id ?? ''}`;
}

/**
 * One environment's sharing rules and the two ways the app page changes them: Share, whose
 * dialog reports through `applied`, and `remove`. A change is sent against the rules as they
 * were `seen`, so one made on rules that moved since is refused rather than applied. The app is
 * read again after a change that took effect; one waiting for approval changed nothing yet.
 */
export function useGrants(app: AppOut, env: EnvironmentOut) {
  const { api, queries, queryClient } = useRouteContext({ from: '/_authed/apps/$appId' });
  const init = { params: { path: { app_id: app.id, environment_id: env.id } } };
  const grants = queries.useQuery('get', PATH, init);
  const [notice, setNotice] = useState<string | null>(null);
  const seen = grants.data ? { grants: grants.data, etag: etagOf(grants.data) } : undefined;

  async function applied(updated: GrantsUpdate, message: string) {
    queryClient.setQueryData(queries.queryOptions('get', PATH, init).queryKey, updated.grants);
    setNotice(message);
    if (updated.state === 'pending') return;
    await queryClient.invalidateQueries({
      queryKey: queries.queryOptions('get', '/v1/apps/{app_id}', {
        params: { path: { app_id: app.id } },
      }).queryKey,
    });
  }

  async function remove(grant: Grant) {
    setNotice(null);
    const updated = await updateGrants(api, { appId: app.id, environmentId: env.id }, withoutGrant(grant), seen);
    await applied(
      updated,
      updated.state === 'pending'
        ? `Waiting for approval, nothing changed yet: ${updated.approvalIds.join(', ')}.`
        : `Removed access for ${who(grant)} (${grant.role}).`,
    );
  }

  return { grants, seen, notice, applied, remove };
}
