import type { ReactNode } from 'react';

export type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral';

/**
 * A status pill: a label on a tint of its tone. The text always carries the meaning; the colour
 * only repeats it.
 */
export function Badge({ tone, children }: { readonly tone: Tone; readonly children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

/** The tone of an app's status; a list's status dot and the badge agree on it. */
export const STATUS_TONE: Readonly<Record<string, Tone>> = {
  active: 'success',
  disabled: 'neutral',
  quarantined: 'danger',
  deleting: 'danger',
};

export function StatusBadge({ status }: { readonly status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? 'warning'}>{status}</Badge>;
}

const APPROVAL: Readonly<Record<string, readonly [Tone, string]>> = {
  pending: ['warning', 'Waiting'],
  approved: ['success', 'Approved'],
  denied: ['danger', 'Rejected'],
  cancelled: ['neutral', 'Withdrawn'],
};

export function approvalWord(state: string): string {
  return APPROVAL[state]?.[1] ?? state;
}

export function ApprovalBadge({ state }: { readonly state: string }) {
  return <Badge tone={APPROVAL[state]?.[0] ?? 'neutral'}>{approvalWord(state)}</Badge>;
}
