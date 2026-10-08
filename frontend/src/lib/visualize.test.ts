import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { Fetcher } from "./auth.ts";
import { createChatStore, parseChatResponse } from "./chat.ts";
import type { ChatData, DatasetPayload, ResponsePlan } from "./types";
import {
  EMPTY_CHAT_DATA,
  MAX_LINE_SERIES,
  chartModel,
  chartSummary,
  formatCell,
  formatMetric,
  parseChatData,
  parseDataset,
  truncationNote,
  visualizationBlocks,
} from "./visualize.ts";

// Shaped exactly like app/agent/datasets.py serializes them.
const LOW_STOCK: DatasetPayload = {
  tool: "get_low_stock_items",
  title: "Low-stock items",
  kind: "records",
  x: null,
  y: [],
  series: null,
  chart: null,
  columns: [
    { key: "item_code", label: "Code", kind: "text" },
    { key: "item_name", label: "Item", kind: "text" },
    { key: "location_name", label: "Location", kind: "text" },
    { key: "quantity", label: "Quantity", kind: "number" },
    { key: "unit", label: "Unit", kind: "text" },
    { key: "effective_threshold", label: "Threshold", kind: "number" },
  ],
  rows: [
    { item_code: "BO-0001", item_name: "Bubble Roll", location_name: "132-1", quantity: "0.000", unit: "roll", effective_threshold: "0" },
    { item_code: "BO-0010", item_name: "Scotch Brite", location_name: "132-1", quantity: "0.000", unit: "pcs", effective_threshold: "0" },
  ],
  total_rows: 2,
  truncated: false,
};

const TOP_VENDORS: DatasetPayload = {
  tool: "procurement_summary",
  title: "Top vendors by spend",
  kind: "categorical",
  x: "vendor_name",
  y: ["total_amount"],
  series: "currency",
  chart: "bar",
  columns: [
    { key: "vendor_name", label: "Vendor", kind: "text" },
    { key: "currency", label: "Currency", kind: "text" },
    { key: "purchase_orders", label: "Orders", kind: "number" },
    { key: "total_amount", label: "Spend", kind: "amount" },
  ],
  rows: [
    { vendor_name: "Sterling Steels", currency: "INR", purchase_orders: 2, total_amount: "3021209.47" },
    { vendor_name: "Acme Inc", currency: "USD", purchase_orders: 1, total_amount: "542.10" },
    { vendor_name: "Meridian", currency: "INR", purchase_orders: 1, total_amount: "395456.74" },
  ],
  total_rows: 3,
  truncated: false,
};

const PO_BY_STATUS: DatasetPayload = {
  ...TOP_VENDORS,
  title: "Purchase orders by status",
  x: "status",
  y: ["purchase_orders", "total_amount"],
  chart: null,
  columns: [
    { key: "status", label: "Status", kind: "text" },
    { key: "currency", label: "Currency", kind: "text" },
    { key: "purchase_orders", label: "Orders", kind: "number" },
    { key: "total_amount", label: "Value", kind: "amount" },
  ],
  rows: [
    { status: "placed", currency: "INR", purchase_orders: 32, total_amount: "4437362.15" },
    { status: "received", currency: "INR", purchase_orders: 21, total_amount: "910663.71" },
  ],
  total_rows: 2,
};

const STOCK_BY_LOCATION: DatasetPayload = {
  tool: "inventory_summary",
  title: "Stock by location",
  kind: "categorical",
  x: "location_name",
  y: ["items_in_stock", "items_out_of_stock"],
  series: null,
  chart: "bar",
  columns: [
    { key: "location_name", label: "Location", kind: "text" },
    { key: "stocked_items", label: "Stocked items", kind: "number" },
    { key: "items_in_stock", label: "In stock", kind: "number" },
    { key: "items_out_of_stock", label: "Out of stock", kind: "number" },
  ],
  rows: [
    { location_name: "103-1", stocked_items: 25, items_in_stock: 14, items_out_of_stock: 11 },
    { location_name: "132-1", stocked_items: 344, items_in_stock: 266, items_out_of_stock: 78 },
  ],
  total_rows: 2,
  truncated: false,
};

const TRANSACTIONS: DatasetPayload = {
  tool: "get_transaction_history",
  title: "Stock transactions",
  kind: "time_series",
  x: "created_at",
  y: ["quantity"],
  series: "item_code",
  chart: "line",
  columns: [
    { key: "created_at", label: "Date", kind: "date" },
    { key: "item_code", label: "Code", kind: "text" },
    { key: "quantity", label: "Quantity", kind: "number" },
  ],
  // Newest first, as the tool returns them.
  rows: [
    { created_at: "2026-10-03 10:00:00", item_code: "A", quantity: "5.000" },
    { created_at: "2026-10-02 10:00:00", item_code: "B", quantity: "2.000" },
    { created_at: "2026-10-01 10:00:00", item_code: "A", quantity: "3.000" },
    { created_at: "not a date", item_code: "A", quantity: "9.000" },
  ],
  total_rows: 4,
  truncated: false,
};

function plan(presentation: ResponsePlan["presentation"]): ResponsePlan {
  return {
    intent: "analytical",
    source: "structured_tool",
    presentation,
    requested_presentation: null,
    confidence: "high",
    clarification_required: false,
    missing: [],
    reason: "test",
    datasets: [],
  };
}

const data = (datasets: DatasetPayload[], metrics: ChatData["metrics"] = []): ChatData => ({ datasets, metrics });
const types = (blocks: ReturnType<typeof visualizationBlocks>) =>
  blocks.map((b) => (b.type === "metrics" ? "metrics" : `${b.type}:${b.dataset.title}`));

// --- parsing --------------------------------------------------------------------------------------

describe("parseChatData", () => {
  it("accepts the backend payload unchanged", () => {
    const payload = data([LOW_STOCK, TOP_VENDORS], [{ label: "Vendors", value: 34 }]);
    assert.deepEqual(parseChatData(JSON.parse(JSON.stringify(payload))), payload);
  });

  it("treats a missing or unusable data field as no data", () => {
    for (const value of [undefined, null, "x", [], { datasets: "x", metrics: 1 }]) {
      assert.deepEqual(parseChatData(value), EMPTY_CHAT_DATA);
    }
  });

  it("drops only the malformed dataset and keeps the valid ones", () => {
    const parsed = parseChatData({
      datasets: [
        { ...LOW_STOCK, columns: "nope" },
        TOP_VENDORS,
        { ...LOW_STOCK, kind: "pie" },
        { ...LOW_STOCK, columns: [{ key: "a", label: "A", kind: "html" }] },
        { ...LOW_STOCK, columns: [{ key: "a", label: "A", kind: "text" }, { key: "a", label: "A", kind: "text" }] },
        null,
      ],
      metrics: [{ label: "Vendors", value: 34 }, { label: "Bad", value: { x: 1 } }, { value: 1 }],
    });
    assert.deepEqual(parsed.datasets.map((d) => d.title), ["Top vendors by spend"]);
    assert.deepEqual(parsed.metrics, [{ label: "Vendors", value: 34 }]);
  });

  it("keeps only declared columns and scalar cells", () => {
    const parsed = parseDataset({
      ...LOW_STOCK,
      rows: [{ ...LOW_STOCK.rows[0], item_id: "uuid-1", quantity: { nested: 1 }, unit: "<b>roll</b>" }, "junk"],
      x: "not_a_column",
      chart: "pie",
    });
    assert.ok(parsed);
    assert.equal(parsed.rows.length, 1);
    assert.ok(!("item_id" in parsed.rows[0]));
    assert.equal(parsed.rows[0].quantity, null);
    assert.equal(parsed.rows[0].unit, "<b>roll</b>"); // text only; React escapes it, nothing is rendered as HTML
    assert.equal(parsed.x, null);
    assert.equal(parsed.chart, null);
  });

  it("never claims more completeness than the backend reported", () => {
    const parsed = parseDataset({ ...LOW_STOCK, total_rows: 120, truncated: true });
    assert.equal(parsed?.truncated, true);
    assert.equal(truncationNote(parsed!), "Showing 2 of 120 rows.");
    assert.equal(truncationNote(LOW_STOCK), null);
  });
});

// --- the registry ---------------------------------------------------------------------------------

describe("visualizationBlocks", () => {
  it("renders nothing for text answers", () => {
    assert.deepEqual(visualizationBlocks(plan("text"), data([LOW_STOCK]), false), []);
  });

  it("never renders data for a change step", () => {
    assert.deepEqual(visualizationBlocks(plan("table"), data([LOW_STOCK]), true), []);
  });

  it("table: every dataset as a table, in order, with the backend's columns", () => {
    const blocks = visualizationBlocks(plan("table"), data([LOW_STOCK, TOP_VENDORS]), false);
    assert.deepEqual(types(blocks), ["table:Low-stock items", "table:Top vendors by spend"]);
    const first = blocks[0];
    assert.ok(first.type === "table");
    assert.deepEqual(first.dataset.columns.map((c) => c.label), ["Code", "Item", "Location", "Quantity", "Unit", "Threshold"]);
  });

  it("table: an empty dataset still gets its (empty) table", () => {
    const empty = { ...LOW_STOCK, rows: [], total_rows: 0 };
    const [block] = visualizationBlocks(plan("table"), data([empty]), false);
    assert.ok(block.type === "table" && block.dataset.rows.length === 0);
  });

  it("summary: only the backend's figures; no figures means the text alone", () => {
    const metrics = [{ label: "Items in catalogue", value: 501 }, { label: "Low-stock positions", value: 149 }];
    assert.deepEqual(types(visualizationBlocks(plan("summary"), data([STOCK_BY_LOCATION], metrics), false)), ["metrics"]);
    assert.deepEqual(visualizationBlocks(plan("summary"), data([LOW_STOCK]), false), []);
    assert.equal(formatMetric({ label: "x", value: 4788746 }), "47,88,746");
  });

  it("chart: only datasets the plan marked are charted", () => {
    const blocks = visualizationBlocks(plan("chart"), data([PO_BY_STATUS, TOP_VENDORS]), false);
    assert.deepEqual(types(blocks), ["chart:Top vendors by spend"]);
  });

  it("chart: with nothing marked, falls back to tables", () => {
    assert.deepEqual(types(visualizationBlocks(plan("chart"), data([PO_BY_STATUS]), false)), ["table:Purchase orders by status"]);
  });

  it("mixed: figures, charts for marked datasets and tables for the rest", () => {
    const blocks = visualizationBlocks(plan("mixed"), data([PO_BY_STATUS, TOP_VENDORS], [{ label: "Vendors", value: 34 }]), false);
    assert.deepEqual(types(blocks), ["metrics", "table:Purchase orders by status", "chart:Top vendors by spend"]);
  });

  it("a numeric table is never turned into a chart on its own", () => {
    assert.deepEqual(types(visualizationBlocks(plan("table"), data([TOP_VENDORS]), false)), ["table:Top vendors by spend"]);
  });
});

// --- chart eligibility and mapping ------------------------------------------------------------------

describe("bar charts", () => {
  it("keeps currencies in separate panels and never sums across them", () => {
    const chart = chartModel(TOP_VENDORS);
    assert.ok(chart?.type === "bar");
    assert.deepEqual(chart.panels.map((p) => p.title), ["INR", "USD"]);
    const [inr, usd] = chart.panels;
    assert.deepEqual(inr.bars.map((b) => [b.label, b.values[0]]), [["Sterling Steels", 3021209.47], ["Meridian", 395456.74]]);
    assert.equal(inr.max, 3021209.47);
    assert.deepEqual(usd.bars.map((b) => b.display[0]), [formatCell(TOP_VENDORS.columns[3], TOP_VENDORS.rows[1])]);
    assert.equal(usd.max, 542.1);
  });

  it("groups same-kind measures on one axis, in the backend's row order", () => {
    const chart = chartModel(STOCK_BY_LOCATION);
    assert.ok(chart?.type === "bar");
    assert.equal(chart.panels.length, 1);
    assert.deepEqual(chart.panels[0].measures.map((m) => m.label), ["In stock", "Out of stock"]);
    assert.deepEqual(chart.panels[0].bars.map((b) => [b.label, ...b.values]), [["103-1", 14, 11], ["132-1", 266, 78]]);
  });

  it("puts counts and money on separate axes", () => {
    const chart = chartModel({ ...PO_BY_STATUS, chart: "bar" });
    assert.ok(chart?.type === "bar");
    assert.deepEqual(chart.panels.map((p) => p.title), ["Orders · INR", "Value · INR"]);
  });

  it("falls back (no chart) when a chart cannot be drawn faithfully", () => {
    assert.equal(chartModel({ ...LOW_STOCK, chart: "bar" }), null); // records are not chartable
    assert.equal(chartModel({ ...TOP_VENDORS, chart: null }), null); // not marked by the plan
    assert.equal(chartModel({ ...TOP_VENDORS, rows: [{ ...TOP_VENDORS.rows[0], total_amount: "-5" }] }), null);
    assert.equal(chartModel({ ...TOP_VENDORS, rows: [{ ...TOP_VENDORS.rows[0], total_amount: null }] }), null);
    const manyCurrencies = TOP_VENDORS.rows.flatMap((row, i) => ["A", "B"].map((c) => ({ ...row, currency: `${c}${i}` })));
    assert.equal(chartModel({ ...TOP_VENDORS, rows: manyCurrencies }), null);
    // The registry then shows the table instead.
    const blocks = visualizationBlocks(plan("chart"), data([{ ...TOP_VENDORS, rows: manyCurrencies }]), false);
    assert.deepEqual(types(blocks), ["table:Top vendors by spend"]);
  });

  it("describes itself in words", () => {
    const chart = chartModel(TOP_VENDORS)!;
    assert.equal(chartSummary(TOP_VENDORS, chart),
      "Bar chart of Top vendors by spend: 3 bars in 2 panels (INR; USD). The data is also available as a table.");
  });
});

describe("line charts", () => {
  it("plots each series along time and skips points it cannot place", () => {
    const chart = chartModel(TRANSACTIONS);
    assert.ok(chart?.type === "line");
    assert.deepEqual(chart.series.map((s) => s.name), ["A", "B"]);
    assert.deepEqual(chart.series[0].points.map((p) => [p.when, p.value]), [["2026-10-01 10:00", 3], ["2026-10-03 10:00", 5]]);
    assert.equal(chart.vMin, 0);
    assert.equal(chart.vMax, 5);
    assert.equal(chart.tMin, Date.parse("2026-10-01T10:00:00Z"));
  });

  it("is not drawn with too many series or too few points", () => {
    const many = Array.from({ length: MAX_LINE_SERIES + 1 }, (_, i) => ({ created_at: `2026-10-0${(i % 9) + 1} 10:00:00`, item_code: `I${i}`, quantity: "1" }));
    assert.equal(chartModel({ ...TRANSACTIONS, rows: many }), null);
    assert.equal(chartModel({ ...TRANSACTIONS, rows: [TRANSACTIONS.rows[0]] }), null);
  });
});

// --- formatting --------------------------------------------------------------------------------------

describe("formatCell", () => {
  const [code, , , quantity] = LOW_STOCK.columns;
  it("formats numbers, money in its own currency, dates and booleans", () => {
    assert.equal(formatCell(quantity, { quantity: "0.000" }), "0");
    assert.equal(formatCell(quantity, { quantity: "1234.5" }), "1,234.5");
    assert.equal(formatCell(TOP_VENDORS.columns[3], TOP_VENDORS.rows[0]), "₹30,21,209.47");
    assert.match(formatCell(TOP_VENDORS.columns[3], TOP_VENDORS.rows[1]), /^(US)?\$542\.10$/);
    assert.equal(formatCell({ key: "d", label: "D", kind: "date" }, { d: "2026-10-01 09:30:00.123" }), "2026-10-01 09:30");
    assert.equal(formatCell({ key: "a", label: "A", kind: "boolean" }, { a: true }), "Yes");
    assert.equal(formatCell(code, { item_code: null }), "—");
    assert.equal(formatCell(quantity, { quantity: "n/a" }), "n/a");
  });
});

// --- chat integration ----------------------------------------------------------------------------------

describe("chat integration", () => {
  it("a malformed data field never holds back the reply", () => {
    const parsed = parseChatResponse({ conversation_id: "c", message: "Here you go.", plan: plan("table"), mutation: null, data: { datasets: [{ junk: true }] } });
    assert.equal(parsed.message, "Here you go.");
    assert.deepEqual(parsed.data, EMPTY_CHAT_DATA);
  });

  it("stores the reply's data with the assistant message", async () => {
    const payload = data([LOW_STOCK]);
    const fetcher = (async (_path: string, options?: { body?: unknown }) => ({
      conversation_id: (options?.body as { conversation_id: string }).conversation_id,
      message: "Two items are low.",
      plan: plan("table"),
      mutation: null,
      data: payload,
    })) as Fetcher;
    const store = createChatStore({ fetcher });
    await store.send("Show me low-stock items");
    const reply = store.getState().messages[1];
    assert.ok(reply.role === "assistant");
    assert.deepEqual(reply.data, payload);
    assert.deepEqual(types(visualizationBlocks(reply.plan, reply.data, reply.mutation !== null)), ["table:Low-stock items"]);
  });
});
