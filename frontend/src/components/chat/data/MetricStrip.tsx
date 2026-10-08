import { formatMetric } from "@/lib/visualize";
import type { Metric } from "@/lib/types";
import styles from "./ReplyData.module.css";

/** Headline figures exactly as the backend computed them. */
export function MetricStrip({ metrics }: { metrics: Metric[] }) {
  return (
    <ul className={styles.metrics} aria-label="Summary figures">
      {metrics.map((metric) => (
        <li key={metric.label} className={styles.metric}>
          <span className={styles.metricLabel}>{metric.label}</span>
          <span className={styles.metricValue}>{formatMetric(metric)}</span>
        </li>
      ))}
    </ul>
  );
}
