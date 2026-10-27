import type { AriaRole, ReactNode } from 'react';

export type NoticeTone = 'info' | 'success' | 'warn' | 'danger';

const CLASS: Readonly<Record<NoticeTone, string>> = {
  info: 'notice notice-info',
  success: 'notice notice-success',
  warn: 'notice notice-warning',
  danger: 'notice notice-danger',
};

interface Props {
  readonly tone: NoticeTone;
  readonly title?: ReactNode;
  readonly role?: AriaRole;
  readonly 'aria-label'?: string;
  readonly children?: ReactNode;
}

export function Notice({ tone, title, role, children, 'aria-label': label }: Props) {
  return (
    <div className={CLASS[tone]} role={role ?? (tone === 'danger' ? 'alert' : 'status')} aria-label={label}>
      {title ? (
        <p>
          <strong>{title}</strong>
        </p>
      ) : null}
      {children}
    </div>
  );
}
