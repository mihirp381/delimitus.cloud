import type { ReactNode } from 'react';

export type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral';

/** A label with a tone. The text always carries the meaning; the colour only repeats it. */
export function Badge({ tone, children }: { readonly tone: Tone; readonly children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

const APP_STATUS_TONE: Readonly<Record<string, Tone>> = {
  active: 'success',
  disabled: 'neutral',
  quarantined: 'danger',
};

export function StatusBadge({ status }: { readonly status: string }) {
  return <Badge tone={APP_STATUS_TONE[status] ?? 'warning'}>{status}</Badge>;
}
