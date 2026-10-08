/**
 * How an assistant reply's structured data is shown (Layer 14).
 *
 * The frontend is a renderer, not an analyst: the backend plan decides the presentation and
 * which datasets are charted, and the backend sends the rows. This module only validates what
 * arrived, picks a renderer from a small explicit registry, and maps rows to marks. It never
 * sums rows, derives metrics, converts currencies or fills in missing values; anything it
 * cannot show faithfully falls back to a table, or to the reply text alone.
 */

import { formatAmount } from "./dashboard.ts";
import type {
  Cell,
  ChartKind,
  ChatData,
  ColumnKind,
  DataKind,
  DatasetColumn,
  DatasetPayload,
  Metric,
  ResponsePlan,
} from "./types";

export const EMPTY_CHAT_DATA: ChatData = { datasets: [], metrics: [] };

/** Bars per panel share one axis, so only same-kind measures are grouped, and at most this many. */
export const MAX_BAR_MEASURES = 3;
/** More panels than this (e.g. many currencies × measures) is shown as a table instead. */
export const MAX_CHART_PANELS = 4;
/** Lines beyond this are not distinguishable by colour; such a dataset is shown as a table. */
export const MAX_LINE_SERIES = 6;

const LOCALE = "en-IN";
const DATA_KINDS: readonly DataKind[] = ["records", "categorical", "time_series"];
const COLUMN_KINDS: readonly ColumnKind[] = ["text", "number", "amount", "date", "boolean"];

// --- parsing: keep only what can be rendered safely --------------------------------------------

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCell(value: unknown): value is Cell {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value))
  );
}

function parseColumns(value: unknown): DatasetColumn[] | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  const columns: DatasetColumn[] = [];
  for (const column of value) {
    if (!isObject(column) || typeof column.key !== "string" || typeof column.label !== "string") return null;
    if (!(COLUMN_KINDS as readonly unknown[]).includes(column.kind)) return null;
    if (columns.some((c) => c.key === column.key)) return null;
    columns.push({ key: column.key, label: column.label, kind: column.kind as ColumnKind });
  }
  return columns;
}

/** A dataset in the expected shape, or null. Cells outside the declared columns are dropped. */
export function parseDataset(value: unknown): DatasetPayload | null {
  if (!isObject(value) || typeof value.tool !== "string" || typeof value.title !== "string") return null;
  if (!(DATA_KINDS as readonly unknown[]).includes(value.kind) || !Array.isArray(value.rows)) return null;
  const columns = parseColumns(value.columns);
  if (!columns) return null;
  const keys = new Set(columns.map((c) => c.key));
  const column = (v: unknown) => (typeof v === "string" && keys.has(v) ? v : null);

  const rows = value.rows.filter(isObject).map((row) => {
    const clean: Record<string, Cell> = {};
    for (const key of keys) clean[key] = isCell(row[key]) ? row[key] : null;
    return clean;
  });
  const total = typeof value.total_rows === "number" && Number.isFinite(value.total_rows) ? value.total_rows : rows.length;
  return {
    tool: value.tool,
    title: value.title,
    kind: value.kind as DataKind,
    x: column(value.x),
    y: Array.isArray(value.y) ? value.y.map(column).filter((k): k is string => k !== null) : [],
    series: column(value.series),
    chart: value.chart === "bar" || value.chart === "line" ? (value.chart as ChartKind) : null,
    columns,
    rows,
    total_rows: Math.max(total, rows.length),
    truncated: value.truncated === true || total > rows.length,
  };
}

function parseMetric(value: unknown): Metric | null {
  if (!isObject(value) || typeof value.label !== "string") return null;
  const v = value.value;
  if (typeof v === "string" || (typeof v === "number" && Number.isFinite(v))) return { label: value.label, value: v };
  return null;
}

/** The reply's data; malformed datasets or metrics are dropped one by one, never failing the reply. */
export function parseChatData(value: unknown): ChatData {
  if (!isObject(value)) return EMPTY_CHAT_DATA;
  const datasets = Array.isArray(value.datasets) ? value.datasets.map(parseDataset) : [];
  const metrics = Array.isArray(value.metrics) ? value.metrics.map(parseMetric) : [];
  return {
    datasets: datasets.filter((d): d is DatasetPayload => d !== null),
    metrics: metrics.filter((m): m is Metric => m !== null),
  };
}

// --- formatting ----------------------------------------------------------------------------------

/** A numeric cell as a number, for drawing only; displayed text always comes from formatCell. */
export function numericValue(cell: Cell): number | null {
  if (typeof cell === "number") return cell;
  if (typeof cell === "string" && /^-?\d+(\.\d+)?$/.test(cell.trim())) return Number(cell);
  return null;
}

const DATE_TIME = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/;

export function formatCell(column: DatasetColumn, row: Record<string, Cell>): string {
  const value = row[column.key];
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (column.kind === "number" || column.kind === "amount") {
    const number = numericValue(value);
    if (number === null) return String(value);
    const currency = column.kind === "amount" && typeof row.currency === "string" ? row.currency : null;
    if (currency) return formatAmount(String(value), currency);
    return new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 3 }).format(number);
  }
  if (column.kind === "date" && typeof value === "string") {
    const match = DATE_TIME.exec(value);
    return match ? `${match[1]} ${match[2]}` : value;
  }
  return String(value);
}

export function formatMetric(metric: Metric): string {
  const number = numericValue(metric.value);
  return number === null ? String(metric.value) : new Intl.NumberFormat(LOCALE).format(number);
}

/** "Showing 50 of 120 rows" when the backend sent fewer rows than the tool returned. */
export function truncationNote(dataset: DatasetPayload): string | null {
  if (!dataset.truncated) return null;
  const fmt = new Intl.NumberFormat(LOCALE);
  return `Showing ${fmt.format(dataset.rows.length)} of ${fmt.format(dataset.total_rows)} rows.`;
}

// --- chart models --------------------------------------------------------------------------------

export type Measure = { key: string; label: string };

export type BarPanel = {
  title: string; // e.g. "Spend · INR"; empty when the dataset title says it all
  measures: Measure[]; // one colour each; a legend is shown when there are two or more
  bars: { label: string; values: (number | null)[]; display: string[] }[];
  max: number;
};

export type LineSeries = { name: string; points: { t: number; value: number; display: string; when: string }[] };

export type ChartModel =
  | { type: "bar"; panels: BarPanel[] }
  | { type: "line"; measure: Measure; series: LineSeries[]; tMin: number; tMax: number; vMin: number; vMax: number };

function columnOf(dataset: DatasetPayload, key: string | null): DatasetColumn | undefined {
  return key === null ? undefined : dataset.columns.find((c) => c.key === key);
}

/** Rows split by the series field, in order of first appearance. Rows are never combined. */
function bySeries(dataset: DatasetPayload): [string, Record<string, Cell>[]][] {
  const groups = new Map<string, Record<string, Cell>[]>();
  for (const row of dataset.rows) {
    const key = dataset.series === null ? "" : String(row[dataset.series] ?? "—");
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  return [...groups.entries()];
}

function barModel(dataset: DatasetPayload): ChartModel | null {
  const x = columnOf(dataset, dataset.x);
  const measures = dataset.y
    .map((key) => columnOf(dataset, key))
    .filter((c): c is DatasetColumn => c !== undefined && (c.kind === "number" || c.kind === "amount"));
  if (!x || measures.length === 0) return null;

  // One axis per panel: measures of different kinds (counts vs money) get their own panels.
  const sameKind = measures.every((m) => m.kind === measures[0].kind) && measures.length <= MAX_BAR_MEASURES;
  const measureSets = sameKind ? [measures] : measures.map((m) => [m]);

  const panels: BarPanel[] = [];
  for (const [seriesValue, rows] of bySeries(dataset)) {
    for (const set of measureSets) {
      const bars = rows.map((row) => ({
        label: formatCell(x, row),
        values: set.map((m) => numericValue(row[m.key])),
        display: set.map((m) => formatCell(m, row)),
      }));
      const values = bars.flatMap((b) => b.values).filter((v): v is number => v !== null);
      if (values.length === 0) continue;
      if (values.some((v) => v < 0)) return null; // bars grow from zero; negatives are left to the table
      const title = [set.length === 1 && measureSets.length > 1 ? set[0].label : "", seriesValue]
        .filter(Boolean)
        .join(" · ");
      panels.push({ title, measures: set.map(({ key, label }) => ({ key, label })), bars, max: Math.max(...values, 0) || 1 });
    }
  }
  if (panels.length === 0 || panels.length > MAX_CHART_PANELS) return null;
  return { type: "bar", panels };
}

/** Naive UTC timestamps from the backend ("2026-10-01 09:30:00") as epoch ms. */
function parseTime(cell: Cell): number | null {
  if (typeof cell !== "string") return null;
  const iso = cell.trim().replace(" ", "T");
  const t = Date.parse(/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`);
  return Number.isFinite(t) ? t : null;
}

function lineModel(dataset: DatasetPayload): ChartModel | null {
  const x = columnOf(dataset, dataset.x);
  const y = columnOf(dataset, dataset.y[0] ?? null);
  if (!x || x.kind !== "date" || !y || (y.kind !== "number" && y.kind !== "amount")) return null;
  const groups = bySeries(dataset);
  if (groups.length > MAX_LINE_SERIES) return null;

  const series: LineSeries[] = groups
    .map(([name, rows]) => ({
      name,
      points: rows
        .map((row) => ({ t: parseTime(row[x.key]), value: numericValue(row[y.key]), display: formatCell(y, row), when: formatCell(x, row) }))
        .filter((p): p is { t: number; value: number; display: string; when: string } => p.t !== null && p.value !== null)
        .sort((a, b) => a.t - b.t), // plotted along time; the table keeps the original order
    }))
    .filter((s) => s.points.length > 0);
  const points = series.flatMap((s) => s.points);
  if (points.length < 2) return null;
  const times = points.map((p) => p.t);
  const values = points.map((p) => p.value);
  return {
    type: "line",
    measure: { key: y.key, label: y.label },
    series,
    tMin: Math.min(...times),
    tMax: Math.max(...times),
    vMin: Math.min(0, ...values),
    vMax: Math.max(...values, 0) || 1,
  };
}

/** A chart for a dataset the plan marked for charting, or null when it cannot be drawn faithfully. */
export function chartModel(dataset: DatasetPayload): ChartModel | null {
  if (dataset.chart === "bar" && dataset.kind === "categorical") return barModel(dataset);
  if (dataset.chart === "line" && dataset.kind === "time_series") return lineModel(dataset);
  return null;
}

// --- the renderer registry -------------------------------------------------------------------------

export type Block =
  | { type: "metrics"; metrics: Metric[] }
  | { type: "table"; dataset: DatasetPayload }
  | { type: "chart"; dataset: DatasetPayload; chart: ChartModel };

/**
 * What to render under a reply, by the plan's presentation:
 *   text          -> nothing (the reply text is the answer)
 *   summary       -> the summary figures
 *   table         -> every dataset as a table
 *   chart         -> the datasets the plan marked as charts (a table when one cannot be drawn);
 *                    if none is marked, every dataset as a table
 *   mixed         -> figures, charts for the marked datasets, tables for the rest
 * Change steps (mutations) never get data blocks: they have their own card.
 */
export function visualizationBlocks(plan: ResponsePlan, data: ChatData, hasMutation: boolean): Block[] {
  const presentation = plan.presentation;
  if (hasMutation || presentation === "text") return [];

  const blocks: Block[] = [];
  if ((presentation === "summary" || presentation === "mixed") && data.metrics.length > 0) {
    blocks.push({ type: "metrics", metrics: data.metrics });
  }
  if (presentation === "summary") return blocks;

  const charting = presentation === "chart" || presentation === "mixed";
  const marked = charting && data.datasets.some((d) => d.chart !== null);
  for (const dataset of data.datasets) {
    if (marked && dataset.chart !== null) {
      const chart = chartModel(dataset);
      blocks.push(chart ? { type: "chart", dataset, chart } : { type: "table", dataset });
    } else if (!(presentation === "chart" && marked)) {
      blocks.push({ type: "table", dataset });
    }
  }
  return blocks;
}

/** One sentence describing a chart, for assistive technology and as a caption. */
export function chartSummary(dataset: DatasetPayload, chart: ChartModel): string {
  if (chart.type === "bar") {
    const bars = chart.panels.reduce((n, p) => n + p.bars.length, 0);
    const split = chart.panels.length > 1 ? ` in ${chart.panels.length} panels (${chart.panels.map((p) => p.title).join("; ")})` : "";
    return `Bar chart of ${dataset.title}: ${bars} bars${split}. The data is also available as a table.`;
  }
  const names = chart.series.map((s) => s.name).filter(Boolean);
  return `Line chart of ${chart.measure.label} over time${names.length ? ` for ${names.join(", ")}` : ""}. The data is also available as a table.`;
}
