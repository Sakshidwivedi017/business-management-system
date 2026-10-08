import type { ReactNode } from "react";

import styles from "./Dashboard.module.css";

export type Column<T> = {
  header: string;
  cell: (row: T) => ReactNode;
  numeric?: boolean;
};

/** A compact read-only table for dashboard panels; scrolls horizontally when narrow. */
export function StatTable<T>({
  caption,
  columns,
  rows,
  rowKey,
  empty,
}: {
  caption: string;
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string;
  empty: string;
}) {
  if (rows.length === 0) return <p className={styles.muted}>{empty}</p>;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.header} scope="col" className={column.numeric ? styles.numeric : undefined}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)}>
              {columns.map((column) => (
                <td key={column.header} className={column.numeric ? styles.numeric : undefined}>
                  {column.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
