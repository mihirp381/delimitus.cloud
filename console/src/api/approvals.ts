import type { GrantInput } from './grants';
import type { components } from './schema';

export type Approval = components['schemas']['Approval'];
export type ApprovalPage = components['schemas']['ApprovalPage'];
export type ApprovalState = Approval['state'];

const STATES: Readonly<Record<ApprovalState, true>> = {
  pending: true,
  approved: true,
  denied: true,
  cancelled: true,
};

export const APPROVAL_STATES = Object.keys(STATES) as readonly ApprovalState[];

export function approvalsSearch(search: Record<string, unknown>): { state?: ApprovalState } {
  const state = search.state;
  return typeof state === 'string' && Object.hasOwn(STATES, state)
    ? { state: state as ApprovalState }
    : {};
}

const ROLES = new Set(['builder', 'user']);
const SUBJECTS = new Set(['user', 'group', 'org']);

function asGrant(value: unknown): GrantInput | null {
  if (typeof value !== 'object' || value === null) return null;
  const g = value as Record<string, unknown>;
  if (typeof g.role !== 'string' || !ROLES.has(g.role)) return null;
  if (typeof g.subject_kind !== 'string' || !SUBJECTS.has(g.subject_kind)) return null;
  const id = g.subject_id ?? null;
  if (id !== null && typeof id !== 'string') return null;
  return {
    role: g.role as GrantInput['role'],
    subject_kind: g.subject_kind as GrantInput['subject_kind'],
    subject_id: id,
  };
}

/**
 * The sharing a `widen_audience` or `agent_share` request asks for: the whole desired set of
 * grants. Null when the payload does not hold one, so the page shows the digest instead.
 */
export function requestedGrants(payload: Approval['payload']): readonly GrantInput[] | null {
  const list = payload.grants;
  if (!Array.isArray(list)) return null;
  const grants = list.map(asGrant);
  return grants.every((g) => g !== null) ? grants : null;
}

/** The grants version an `agent_share` request replaces, when the payload names one. */
export function replacedVersion(payload: Approval['payload']): number | null {
  const v = payload.grants_version;
  return typeof v === 'number' && Number.isInteger(v) ? v : null;
}

export function grantText(g: GrantInput): string {
  if (g.subject_kind === 'org') return `Everyone in the organisation (${g.role})`;
  return `${g.subject_kind === 'user' ? 'User' : 'Group'} ${g.subject_id ?? ''} (${g.role})`;
}
