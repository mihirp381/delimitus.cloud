import { type ReactNode, useId } from 'react';

interface Props {
  readonly title: string;
  /** What you can do to the whole section, shown at the end of its heading row. */
  readonly actions?: ReactNode;
  readonly className?: string;
  readonly children: ReactNode;
}

/** One card on an app's tab: a heading, and what the tab's environment holds under it. */
export function Section({ title, actions, className, children }: Props) {
  const headingId = useId();
  return (
    <section className={className ? `panel ${className}` : 'panel'} aria-labelledby={headingId}>
      {actions ? (
        <div className="panel-head">
          <h2 id={headingId}>{title}</h2>
          {actions}
        </div>
      ) : (
        <h2 id={headingId}>{title}</h2>
      )}
      {children}
    </section>
  );
}
