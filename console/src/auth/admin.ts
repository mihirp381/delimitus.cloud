import type { components } from '../api/schema';

export type Whoami = components['schemas']['Whoami'];

/**
 * Whether the console shows admin-only screens (today: the audit log) to this caller. This is
 * the one place that decides it, and it only hides things: the API refuses a non-admin anyway.
 *
 * It answers from `whoami`'s `role` (A4b), which the API reads from the directory on every
 * request. Until whoami has loaded, and for a credential with no role (an agent's, or a
 * deactivated user's), it answers false.
 */
export function isAdmin(me: Whoami | undefined): boolean {
  return me?.role === 'admin';
}
