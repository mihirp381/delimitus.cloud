import type { components } from '../api/schema';

export type Whoami = components['schemas']['Whoami'];

/**
 * Whether the console shows admin-only screens (today: the audit log) to this caller. This is
 * the one place that decides it, and it only hides things: the API refuses a non-admin anyway.
 *
 * `whoami` has no role yet (asked of A4 and B5), so every signed-in caller sees every screen and
 * a non-admin gets the API's `403`. When the role lands, this answers from it, and `undefined`
 * (whoami not loaded yet) should answer false.
 */
export function isAdmin(_me: Whoami | undefined): boolean {
  return true;
}
