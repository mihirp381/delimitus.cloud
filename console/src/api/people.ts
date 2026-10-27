import { type ApiClient, must } from './client';
import type { components } from './schema';

export type UnlinkedLogin = components['schemas']['UnlinkedLogin'];
export type UserMatch = components['schemas']['UserMatch'];
export type Linked = components['schemas']['Linked'];
export type SignInMode = components['schemas']['Whoami']['sign_in_mode'];

/** Why a login matched no person, in the words of `ssc logins list`. */
export const REASON_TEXT: Readonly<Record<UnlinkedLogin['reason'], string>> = {
  no_match: 'no one with this email',
  ambiguous_email: 'several people, same email',
};

/** What `ssc logins list` says of a login that cannot be linked. */
export const NOT_LINKABLE = 'no: fix it in the directory';

/**
 * Ties a login no person could be found for to an active person; their next login with it signs
 * them in. Org admins only, never in an agent session. The API answers NOT_FOUND when the login
 * is gone or already linked, and REFERENCE_NOT_FOUND when the person is not active.
 */
export async function linkLogin(api: ApiClient, unlinkedLoginId: string, userId: string): Promise<Linked> {
  return must(
    await api.POST('/v1/unlinked-logins/{unlinked_login_id}/link', {
      params: { path: { unlinked_login_id: unlinkedLoginId } },
      body: { user_id: userId },
    }),
  );
}

export type RosterFilter = 'active' | 'invited' | 'all';

/** One page of the roster, by name: `q` is part of a name or email, ignoring case. Org admins only. */
export async function rosterPage(
  api: ApiClient,
  q: string,
  filter: RosterFilter,
  cursor: string | null,
  signal?: AbortSignal,
): Promise<components['schemas']['UserMatches']> {
  const query = {
    ...(q ? { q } : {}),
    ...(filter === 'all' ? {} : { status: filter }),
    ...(cursor ? { cursor } : {}),
  };
  return must(await api.GET('/v1/users', { params: { query }, signal }));
}

/** An invited member, or the person who already has the address. Social orgs, admins only. */
export async function invitePerson(api: ApiClient, email: string): Promise<UserMatch> {
  return must(await api.POST('/v1/users/invite', { body: { email } }));
}

export async function deactivatePerson(api: ApiClient, userId: string): Promise<UserMatch> {
  return must(await api.POST('/v1/users/{user_id}/deactivate', { params: { path: { user_id: userId } } }));
}

export async function reactivatePerson(api: ApiClient, userId: string): Promise<UserMatch> {
  return must(await api.POST('/v1/users/{user_id}/reactivate', { params: { path: { user_id: userId } } }));
}
