import { formatCell, truncationNote } from "@/lib/visualize";
import type { DatasetPayload } from "@/lib/types";
import styles from "./ReplyData.module.css";

const NUMERIC = new Set(["number", "amount"]);

/** A dataset as a semantic table; scrolls sideways only when its columns do not fit. */
export function DataTable({ dataset, showCaption = true }: { dataset: DatasetPayload; showCaption?: boolean }) {
  const note = truncationNote(dataset);
  if (dataset.rows.length === 0) {
    return <p className={styles.muted}>{dataset.title}: no rows were returned.</p>;
  }
  return (
    <div className={styles.tableBlock}>
      {/* A focusable region so keyboard users can scroll a wide table. */}
      <div className={styles.tableScroll} role="region" aria-label={dataset.title} tabIndex={0}>
        <table className={styles.table}>
          <caption className={showCaption ? styles.caption : "sr-only"}>{dataset.title}</caption>
          <thead>
            <tr>
              {dataset.columns.map((column) => (
                <th key={column.key} scope="col" className={NUMERIC.has(column.kind) ? styles.numeric : undefined}>
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {dataset.rows.map((row, index) => (
              <tr key={index}>
                {dataset.columns.map((column) => (
                  <td key={column.key} className={NUMERIC.has(column.kind) ? styles.numeric : undefined}>
                    {formatCell(column, row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {note && <p className={styles.note}>{note}</p>}
    </div>
  );
}
