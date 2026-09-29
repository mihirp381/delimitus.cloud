import { type ApiClient, must } from './client';

/** One search result: an id to act on, a name to show, and why it cannot be picked, if so. */
export interface Match {
  readonly id: string;
  readonly label: string;
  readonly detail?: string;
  readonly unavailable?: string;
}

export const USER_ID_PATTERN = /^usr_[a-z0-9]{20}$/;
export const GROUP_ID_PATTERN = /^grp_[a-z0-9]{20}$/;
const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+$/;

/** The org's groups with exactly this name, ignoring case. Anyone who may share can ask. */
export async function findGroups(api: ApiClient, name: string): Promise<readonly Match[]> {
  const found = must(await api.GET('/v1/groups', { params: { query: { name } } }));
  return found.groups.map((g) => ({
    id: g.id,
    label: g.name,
    detail: `${g.member_count} active ${g.member_count === 1 ? 'member' : 'members'}`,
  }));
}

/** The org's people with exactly this email, ignoring case. Org admins only. */
export async function findPeople(api: ApiClient, email: string): Promise<readonly Match[]> {
  if (!EMAIL_PATTERN.test(email)) throw new Error('Type a whole email address, or a usr_ id.');
  const found = must(await api.GET('/v1/users', { params: { query: { email } } }));
  return found.users.map((u) => ({
    id: u.id,
    label: u.display_name,
    detail: u.email,
    unavailable: u.status === 'active' ? undefined : 'deactivated',
  }));
}
