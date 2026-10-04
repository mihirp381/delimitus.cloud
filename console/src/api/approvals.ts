import { type ApiClient, must } from './client';
import type { GrantInput } from './grants';
import type { components } from './schema';

export type Approval = components['schemas']['Approval'];
export type ApprovalPage = components['schemas']['ApprovalPage'];
export type ApprovalDetail = components['schemas']['ApprovalDetail'];
export type ApprovalDecided = components['schemas']['ApprovalDecided'];
export type DiffGrant = components['schemas']['DiffGrant'];
export type ApprovalState = Approval['state'];
export type Outcome = components['schemas']['PersonDecisionIn']['outcome'];

export const REASON_MAX = 500;

const STATES: Readonly<Record<ApprovalState, true>> = {
  pending: true,
  approved: true,
  denied: true,
  cancelled: true,
};

export const APPROVAL_STATES = Object.keys(STATES) as readonly ApprovalState[];

export interface ApprovalsSearch {
  readonly state?: ApprovalState;
  readonly view?: 'all';
}

/** The inbox is the default view; `view=all` lists every request the caller may see. */
export function approvalsSearch(search: Record<string, unknown>): ApprovalsSearch {
  const { state, view } = search;
  const known = typeof state === 'string' && Object.hasOwn(STATES, state);
  return {
    ...(known ? { state: state as ApprovalState } : {}),
    ...(view === 'all' ? { view: 'all' as const } : {}),
  };
}

/** Records the caller's decision; the reason is shown to the requester. */
export async function decideApproval(
  api: ApiClient,
  approvalId: string,
  outcome: Outcome,
  reason: string,
): Promise<ApprovalDecided> {
  return must(
    await api.POST('/v1/approvals/{approval_id}/decide', {
      params: { path: { approval_id: approvalId } },
      body: { outcome, reason, channel: 'console' },
    }),
  );
}

export async function cancelApproval(api: ApiClient, approvalId: string): Promise<Approval> {
  return must(
    await api.POST('/v1/approvals/{approval_id}/cancel', {
      params: { path: { approval_id: approvalId } },
      body: { reason: 'Withdrawn by the requester.', channel: 'console' },
    }),
  );
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

export function diffGrantText(g: DiffGrant): string {
  if (g.subject_kind === 'org') return `Everyone in the organisation (${g.role})`;
  const name = g.subject_name ?? g.subject_id ?? '';
  return `${g.subject_kind === 'user' ? 'User' : 'Group'} ${name} (${g.role})`;
}

const NOT_APPLIED: Readonly<Record<string, string>> = {
  stale: 'the sharing rules changed after it was asked',
  rules: 'it now breaks a sharing rule',
  app_not_active: 'the app is not active',
  requester_cannot_build: 'the requester can no longer build the app',
  payload_invalid: 'the stored request cannot be read',
};

/** What an approval did once it was approved: whether the grant was applied, and why not. */
export function appliedText(d: ApprovalDecided): string {
  switch (d.applied) {
    case 'applied':
      return 'Approved, and the sharing change is now in force.';
    case 'waiting':
      return 'Approved. The change applies when the other approvals it needs are in.';
    case 'not_applied':
      return `Approved, but the change was not applied (${NOT_APPLIED[d.applied_reason ?? ''] ?? 'it is out of date'}). Ask again.`;
    default:
      return 'Approved.';
  }
}

/** The connection an `exceed_ceiling` request names, or the subject key when the payload lacks one. */
export function connectionName(a: Pick<Approval, 'payload' | 'subject_key'>): string {
  const name = a.payload.connection;
  return typeof name === 'string' ? name : a.subject_key;
}
