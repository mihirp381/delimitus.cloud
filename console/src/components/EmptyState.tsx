import type { ReactNode } from 'react';

interface Props {
  /** What is missing, for example "No apps yet." */
  readonly children: ReactNode;
  /** What to do next. */
  readonly hint?: ReactNode;
}

/** Shown in place of a list with nothing in it: what is missing, then what to do about it. */
export function EmptyState({ children, hint }: Props) {
  return (
    <div className="empty-state">
      <span className="empty-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" focusable="false">
          <path d="M3.5 13.5 6.2 5.6A1.5 1.5 0 0 1 7.6 4.6h8.8a1.5 1.5 0 0 1 1.4 1l2.7 7.9" />
          <path d="M3.5 13.5V18A1.5 1.5 0 0 0 5 19.5h14a1.5 1.5 0 0 0 1.5-1.5v-4.5h-5.2a1 1 0 0 0-.9.6 2.6 2.6 0 0 1-4.8 0 1 1 0 0 0-.9-.6Z" />
        </svg>
      </span>
      <p className="empty">{children}</p>
      {hint ? <p className="empty-hint">{hint}</p> : null}
    </div>
  );
}
