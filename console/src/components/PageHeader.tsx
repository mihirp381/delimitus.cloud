import { createContext, type ReactNode, useContext } from 'react';
import { createPortal } from 'react-dom';

/**
 * Where a page's actions go: the right-hand end of the page bar, which the shell owns. `null`
 * until the bar has mounted; `undefined` outside the shell, where the actions stay in the header.
 */
export const PageActionsSlot = createContext<HTMLElement | null | undefined>(undefined);

/** A page's actions, shown in the page bar beside the breadcrumbs. */
export function PageActions({ children }: { readonly children: ReactNode }) {
  const slot = useContext(PageActionsSlot);
  if (slot === undefined) return <div className="page-head-actions">{children}</div>;
  return slot ? createPortal(children, slot) : null;
}

export const PURPOSE_MAX = 140;

interface Props {
  readonly title: ReactNode;
  /** One sentence, at most PURPOSE_MAX characters (test/page-header.test.ts). */
  readonly purpose?: string;
  /** What else the page needs said, under "About this page". */
  readonly about?: ReactNode;
  /** A status pill shown beside the title. */
  readonly status?: ReactNode;
  /** A line of facts under the title: who owns it, its id. */
  readonly meta?: ReactNode;
  /** The page's actions; they are shown in the page bar. */
  readonly children?: ReactNode;
}

/** The top of every page: its title and what it is for. What you can do from it sits in the page bar. */
export function PageHeader({ title, purpose, about, status, meta, children }: Props) {
  return (
    <div className="page-head">
      <div className="page-head-main">
        <div className="page-title">
          <h1>{title}</h1>
          {status}
        </div>
        {purpose ? <p className="page-purpose">{purpose}</p> : null}
        {meta}
        {about ? (
          <details className="page-about">
            <summary>About this page</summary>
            <div className="page-about-body">{about}</div>
          </details>
        ) : null}
      </div>
      {children ? <PageActions>{children}</PageActions> : null}
    </div>
  );
}
