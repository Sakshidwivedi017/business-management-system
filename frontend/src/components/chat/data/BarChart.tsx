import type { BarPanel } from "@/lib/visualize";
import styles from "./ReplyData.module.css";

/** Horizontal bars from a zero baseline, one panel per currency / measure; values are labelled at the bar end. */
export function BarChart({ panels }: { panels: BarPanel[] }) {
  return (
    <div className={styles.panels}>
      {panels.map((panel, index) => (
        <div key={`${panel.title}-${index}`} className={styles.panel}>
          {panel.title && <p className={styles.panelTitle}>{panel.title}</p>}
          {panel.measures.length > 1 && <Legend names={panel.measures.map((m) => m.label)} />}
          <ul className={styles.bars}>
            {panel.bars.map((bar, barIndex) => (
              <li
                key={barIndex}
                className={styles.barRow}
                title={`${bar.label}: ${panel.measures.map((m, i) => `${m.label} ${bar.display[i]}`).join(", ")}`}
              >
                <span className={styles.barLabel}>{bar.label}</span>
                <span className={styles.barTracks}>
                  {bar.values.map((value, i) => (
                    <span key={i} className={styles.barTrack}>
                      <span className={styles.barArea}>
                        <span
                          className={styles.bar}
                          style={{
                            width: value === null ? 0 : `${Math.max((value / panel.max) * 100, value > 0 ? 1 : 0)}%`,
                            background: `var(--chart-${(i % 6) + 1})`,
                          }}
                        />
                      </span>
                      <span className={styles.barValue}>{bar.display[i]}</span>
                    </span>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

export function Legend({ names }: { names: string[] }) {
  return (
    <ul className={styles.legend} aria-hidden="true">
      {names.map((name, i) => (
        <li key={name}>
          <span className={styles.swatch} style={{ background: `var(--chart-${(i % 6) + 1})` }} />
          {name}
        </li>
      ))}
    </ul>
  );
}
