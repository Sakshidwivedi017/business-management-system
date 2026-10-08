import type { AssistantMessage } from "@/lib/chat";
import { chartSummary, truncationNote, visualizationBlocks } from "@/lib/visualize";
import { BarChart } from "./BarChart";
import { DataTable } from "./DataTable";
import { LineChart } from "./LineChart";
import { MetricStrip } from "./MetricStrip";
import styles from "./ReplyData.module.css";

/** Tables, figures and charts under a reply, as the backend plan presents them. */
export function ReplyData({ message }: { message: AssistantMessage }) {
  const blocks = visualizationBlocks(message.plan, message.data, message.mutation !== null);
  if (blocks.length === 0) return null;

  return (
    <div className={styles.blocks}>
      {blocks.map((block, index) => {
        if (block.type === "metrics") return <MetricStrip key={index} metrics={block.metrics} />;
        if (block.type === "table") return <DataTable key={index} dataset={block.dataset} />;
        const summary = chartSummary(block.dataset, block.chart);
        const note = truncationNote(block.dataset);
        return (
          <figure key={index} className={styles.figure} aria-label={summary}>
            <figcaption className={styles.caption}>{block.dataset.title}</figcaption>
            {block.chart.type === "bar" ? <BarChart panels={block.chart.panels} /> : <LineChart chart={block.chart} />}
            {note && <p className={styles.note}>{note}</p>}
            <details className={styles.details}>
              <summary>Show data table</summary>
              <DataTable dataset={block.dataset} showCaption={false} />
            </details>
          </figure>
        );
      })}
    </div>
  );
}
