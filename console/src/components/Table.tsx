import type { ReactNode } from 'react';

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
  readonly empty: ReactNode;
}

export function Table<T>({ caption, columns, rows, rowKey, empty }: Props<T>) {
  if (rows.length === 0) return <p className="empty">{empty}</p>;
  return (
    <div className="table-wrap">
      <table className="table">
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
