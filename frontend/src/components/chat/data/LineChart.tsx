import type { ChartModel } from "@/lib/visualize";
import { Legend } from "./BarChart";
import styles from "./ReplyData.module.css";

type LineModel = Extract<ChartModel, { type: "line" }>;

const WIDTH = 640;
const HEIGHT = 220;
const PAD = { top: 12, right: 16, bottom: 28, left: 64 };
const LOCALE = "en-IN";

/** A time-series line per series on one shared axis; every point has a hover label. */
export function LineChart({ chart }: { chart: LineModel }) {
  const plotW = WIDTH - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const tSpan = chart.tMax - chart.tMin || 1;
  const vSpan = chart.vMax - chart.vMin || 1;
  const px = (t: number) => PAD.left + ((t - chart.tMin) / tSpan) * plotW;
  const py = (v: number) => PAD.top + (1 - (v - chart.vMin) / vSpan) * plotH;
  const number = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 2 });
  const date = (t: number) => new Date(t).toISOString().slice(0, 10);
  const named = chart.series.length > 1;

  return (
    <div className={styles.panel}>
      {named && <Legend names={chart.series.map((s) => s.name)} />}
      <svg className={styles.lineSvg} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="presentation">
        {[chart.vMin, (chart.vMin + chart.vMax) / 2, chart.vMax].map((v) => (
          <g key={v}>
            <line x1={PAD.left} x2={WIDTH - PAD.right} y1={py(v)} y2={py(v)} className={styles.grid} />
            <text x={PAD.left - 8} y={py(v)} className={styles.axisLabel} textAnchor="end" dominantBaseline="middle">
              {number.format(v)}
            </text>
          </g>
        ))}
        <text x={PAD.left} y={HEIGHT - 8} className={styles.axisLabel}>
          {date(chart.tMin)}
        </text>
        <text x={WIDTH - PAD.right} y={HEIGHT - 8} className={styles.axisLabel} textAnchor="end">
          {date(chart.tMax)} (UTC)
        </text>
        {chart.series.map((series, i) => {
          const color = `var(--chart-${(i % 6) + 1})`;
          return (
            <g key={series.name || i}>
              {series.points.length > 1 && (
                <polyline
                  points={series.points.map((p) => `${px(p.t)},${py(p.value)}`).join(" ")}
                  fill="none"
                  stroke={color}
                  strokeWidth={2}
                  strokeLinejoin="round"
                />
              )}
              {series.points.map((p, j) => (
                <circle key={j} cx={px(p.t)} cy={py(p.value)} r={4} fill={color} className={styles.point}>
                  <title>{`${series.name ? `${series.name} · ` : ""}${p.when}: ${chart.measure.label} ${p.display}`}</title>
                </circle>
              ))}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
