import type { Kpi } from "@/lib/dashboard";
import styles from "./Dashboard.module.css";

export function KpiGrid({ kpis }: { kpis: Kpi[] }) {
  return (
    <ul className={styles.kpis} aria-label="Key figures">
      {kpis.map((kpi) => (
        <li key={kpi.id} className={styles.kpi} data-tone={kpi.tone}>
          <span className={styles.kpiLabel}>{kpi.label}</span>
          <span className={styles.kpiValue}>{kpi.value}</span>
          <span className={styles.kpiDetail}>{kpi.detail}</span>
        </li>
      ))}
    </ul>
  );
}
