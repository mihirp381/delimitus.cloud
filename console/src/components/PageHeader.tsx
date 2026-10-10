import type { ReactNode } from 'react';

interface Props {
  readonly title: ReactNode;
  /** One line saying what the page is for. */
  readonly purpose?: ReactNode;
  /** A status pill shown beside the title. */
  readonly status?: ReactNode;
  /** The page's primary action, or the controls that narrow its list. */
  readonly children?: ReactNode;
}

/** The top of every page: its title, what it is for, and what you can do from it. */
export function PageHeader({ title, purpose, status, children }: Props) {
  return (
    <div className="page-head">
      <div className="page-head-main">
        <div className="page-title">
          <h1>{title}</h1>
          {status}
        </div>
        {purpose ? <p className="page-purpose">{purpose}</p> : null}
      </div>
      {children ? <div className="page-head-actions">{children}</div> : null}
    </div>
  );
}
