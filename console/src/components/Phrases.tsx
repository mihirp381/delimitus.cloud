import { Fragment } from 'react';

interface Props {
  readonly items: readonly string[];
  /** Shown when there are no items. */
  readonly none?: string;
}

/** Short phrases after one another, each kept on one line: a narrow column breaks between them, never inside one. */
export function Phrases({ items, none = 'None' }: Props) {
  if (items.length === 0) return <>{none}</>;
  return (
    <>
      {items.map((item, i) => (
        <Fragment key={item}>
          {i > 0 ? ', ' : null}
          <span className="nowrap">{item}</span>
        </Fragment>
      ))}
    </>
  );
}
