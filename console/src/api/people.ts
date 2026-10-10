import { type ApiClient, must } from './client';
import type { components } from './schema';

export type UnlinkedLogin = components['schemas']['UnlinkedLogin'];
export type UserMatch = components['schemas']['UserMatch'];
export type GroupMatch = components['schemas']['GroupMatch'];
export type Linked = components['schemas']['Linked'];

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

const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+$/;

/** The org's people with exactly this email, ignoring case, deactivated ones included. */
export async function peopleByEmail(api: ApiClient, email: string): Promise<readonly UserMatch[]> {
  if (!EMAIL_PATTERN.test(email)) throw new Error('Type a whole email address.');
  return must(await api.GET('/v1/users', { params: { query: { email } } })).users;
}

/** The org's groups with exactly this name, ignoring case. */
export async function groupsByName(api: ApiClient, name: string): Promise<readonly GroupMatch[]> {
  return must(await api.GET('/v1/groups', { params: { query: { name } } })).groups;
}
