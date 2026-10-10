import type { ReactNode } from 'react';
import { EmptyState } from './EmptyState';

export interface Column<T> {
  readonly header: string;
  readonly cell: (row: T) => ReactNode;
  readonly className?: string;
}

interface Props<T> {
  readonly caption: string;
  readonly columns: readonly Column<T>[];
  readonly rows: readonly T[];
  readonly rowKey: (row: T) => string;
  /** Shown instead of the table when there are no rows: what is missing. */
  readonly empty: ReactNode;
  /** With `empty`: what to do next. */
  readonly emptyHint?: ReactNode;
  /** Tighter cells, for a table with many columns. */
  readonly dense?: boolean;
}

/** Rows as a card: a quiet heading row, a hairline between rows, and the row under the pointer lit. */
export function Table<T>({ caption, columns, rows, rowKey, empty, emptyHint, dense }: Props<T>) {
  if (rows.length === 0) return <EmptyState hint={emptyHint}>{empty}</EmptyState>;
  return (
    <div className="table-wrap">
      <table className={dense ? 'table table-dense' : 'table'}>
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.header} scope="col" className={c.className}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((c) => (
                <td key={c.header} className={c.className}>
                  {c.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
