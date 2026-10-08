/**
 * Dashboard data (GET /api/dashboard) and what the page shows from it.
 *
 * The backend decides which sections a user receives; this module only presents what came
 * back. KPIs and panels are derived from the sections that are present, so nothing is shown
 * that the response does not contain. The role only chooses wording and panel order.
 */

import { ApiError } from "./api.ts";
import type { Fetcher } from "./auth.ts";
import { isRole } from "./roles.ts";
import type {
  BusinessAnalytics,
  DashboardResponse,
  InventoryOverview,
  ProcurementOverview,
  Role,
} from "./types";

const LOCALE = "en-IN";
const UNEXPECTED_MESSAGE = "The dashboard returned an unexpected response. Please try again.";
const FAILED_MESSAGE = "Unable to load the dashboard. Please try again.";

// --- loading -------------------------------------------------------------------------------

export type DashboardState =
  | { status: "loading" }
  | { status: "ready"; data: DashboardResponse }
  | { status: "error"; message: string };

export const INITIAL_DASHBOARD_STATE: DashboardState = { status: "loading" };

/**
 * Fetch the dashboard with the session's authFetch. A 401 has already ended the session
 * there; it still resolves to an error state here. An aborted load rethrows.
 */
export async function loadDashboard(authFetch: Fetcher, signal?: AbortSignal): Promise<DashboardState> {
  try {
    const body = await authFetch<unknown>("/api/dashboard", { signal });
    const data = parseDashboard(body);
    return data ? { status: "ready", data } : { status: "error", message: UNEXPECTED_MESSAGE };
  } catch (err) {
    if (signal?.aborted) throw err;
    return { status: "error", message: err instanceof ApiError ? err.message : FAILED_MESSAGE };
  }
}

// --- parsing: accept only what the page can render -------------------------------------------

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function rows<T>(value: unknown, valid: (row: Record<string, unknown>) => boolean): T[] {
  return Array.isArray(value) ? (value.filter((row) => isObject(row) && valid(row)) as T[]) : [];
}

const isAmount = (value: unknown) => value === null || typeof value === "string";

function parseInventory(value: unknown): InventoryOverview | null {
  if (!isObject(value) || !isObject(value.low_stock)) return null;
  const { total_items, active_items, active_locations, low_stock } = value;
  if (!isCount(total_items) || !isCount(active_items) || !isCount(active_locations) || !isCount(low_stock.count)) {
    return null;
  }
  return {
    total_items,
    active_items,
    active_locations,
    low_stock: { count: low_stock.count, fallback_threshold: String(low_stock.fallback_threshold ?? "0") },
  };
}

function parseProcurement(value: unknown): ProcurementOverview | null {
  if (!isObject(value) || !isCount(value.total_vendors) || !isCount(value.active_vendors)) return null;
  return {
    total_vendors: value.total_vendors,
    active_vendors: value.active_vendors,
    purchase_orders_by_status: rows(value.purchase_orders_by_status, (r) =>
      typeof r.status === "string" && typeof r.currency === "string" && isCount(r.purchase_orders) && isAmount(r.total_amount),
    ),
    open_purchase_orders: rows(value.open_purchase_orders, (r) =>
      typeof r.currency === "string" && isCount(r.purchase_orders) && isAmount(r.total_amount) &&
      isCount(r.lines_pending_delivery),
    ),
  };
}

function parseAnalytics(value: unknown): BusinessAnalytics | null {
  if (!isObject(value)) return null;
  const recent = isObject(value.recent_transactions) ? value.recent_transactions : {};
  return {
    stock_by_location: rows(value.stock_by_location, (r) =>
      typeof r.location_name === "string" && isCount(r.items_in_stock) && isCount(r.items_out_of_stock) &&
      isCount(r.stocked_items),
    ),
    recent_transactions: {
      days: isCount(recent.days) ? recent.days : 0,
      by_type: rows(recent.by_type, (r) => typeof r.transaction_type === "string" && isCount(r.transactions)),
    },
    top_vendors_by_spend: rows(value.top_vendors_by_spend, (r) =>
      typeof r.vendor_name === "string" && typeof r.currency === "string" && isCount(r.purchase_orders) &&
      isAmount(r.total_amount),
    ),
  };
}

/** The response in the expected shape, with malformed sections or rows dropped; null if unusable. */
export function parseDashboard(body: unknown): DashboardResponse | null {
  if (!isObject(body) || !isObject(body.user)) return null;
  const { full_name, role } = body.user;
  if (typeof full_name !== "string" || !isRole(role)) return null;
  return {
    user: { full_name, role },
    inventory: parseInventory(body.inventory),
    procurement: parseProcurement(body.procurement),
    analytics: parseAnalytics(body.analytics),
  };
}

// --- formatting ------------------------------------------------------------------------------

export function formatCount(value: number): string {
  return new Intl.NumberFormat(LOCALE).format(value);
}

/** A decimal amount in its own currency. Amounts in different currencies are never combined. */
export function formatAmount(amount: string | null, currency: string): string {
  if (amount === null) return "—";
  const value = Number(amount);
  if (!Number.isFinite(value)) return `${currency} ${amount}`;
  try {
    return new Intl.NumberFormat(LOCALE, { style: "currency", currency, maximumFractionDigits: 2 }).format(value);
  } catch {
    return `${currency} ${new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 2 }).format(value)}`;
  }
}

const STATUS_LABELS: Record<string, string> = {
  placed: "Placed",
  partial: "Partially received",
  received: "Received",
};

export function humanize(value: string): string {
  const text = value.replace(/_/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : value;
}

export function poStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? humanize(status);
}

// --- header ----------------------------------------------------------------------------------

export function greeting(date: Date): string {
  const hour = date.getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

export function firstName(fullName: string): string {
  return fullName.trim().split(/\s+/)[0] ?? "";
}

export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  inventory_manager: "Stock levels and locations across your inventory.",
  procurement_manager: "Purchase orders, vendors and the stock signals behind them.",
  owner: "A business-wide view of inventory and procurement.",
};

// --- KPIs ------------------------------------------------------------------------------------

export type KpiId = "active_items" | "low_stock" | "locations" | "open_purchase_orders" | "vendors";

export type Kpi = {
  id: KpiId;
  label: string;
  value: string;
  detail: string;
  tone: "neutral" | "warning";
};

/** Open purchase order value per currency, e.g. "₹47,88,746.73 · US$542.10". */
export function openValueByCurrency(procurement: ProcurementOverview): string {
  return procurement.open_purchase_orders
    .map((row) => formatAmount(row.total_amount, row.currency))
    .join(" · ");
}

/** KPIs for the sections present in the response, in a fixed order. */
export function dashboardKpis(data: DashboardResponse): Kpi[] {
  const kpis: Kpi[] = [];
  const { inventory, procurement } = data;
  if (inventory) {
    kpis.push(
      {
        id: "active_items",
        label: "Active items",
        value: formatCount(inventory.active_items),
        detail: `${formatCount(inventory.total_items)} items in the catalogue`,
        tone: "neutral",
      },
      {
        id: "low_stock",
        label: "Low-stock positions",
        value: formatCount(inventory.low_stock.count),
        detail: "Item–location stock at or below its minimum level",
        tone: inventory.low_stock.count > 0 ? "warning" : "neutral",
      },
      {
        id: "locations",
        label: "Active locations",
        value: formatCount(inventory.active_locations),
        detail: "Warehouses and stores holding stock",
        tone: "neutral",
      },
    );
  }
  if (procurement) {
    const open = procurement.open_purchase_orders.reduce((sum, row) => sum + row.purchase_orders, 0);
    kpis.push(
      {
        id: "open_purchase_orders",
        label: "Open purchase orders",
        value: formatCount(open),
        detail: open > 0 ? `Open value ${openValueByCurrency(procurement)}` : "No orders awaiting delivery",
        tone: "neutral",
      },
      {
        id: "vendors",
        label: "Active vendors",
        value: formatCount(procurement.active_vendors),
        detail: `${formatCount(procurement.total_vendors)} vendors on record`,
        tone: "neutral",
      },
    );
  }
  return kpis;
}

// --- panels ----------------------------------------------------------------------------------

export type PanelId = "inventory_operations" | "procurement_overview" | "business_overview";

const ROLE_PANELS: Record<Role, PanelId[]> = {
  inventory_manager: ["inventory_operations"],
  procurement_manager: ["procurement_overview"],
  owner: ["business_overview", "procurement_overview"],
};

const PANEL_SECTION: Record<PanelId, keyof Omit<DashboardResponse, "user">> = {
  inventory_operations: "inventory",
  procurement_overview: "procurement",
  business_overview: "analytics",
};

/** The role's panels whose data is in the response. */
export function dashboardPanels(data: DashboardResponse): PanelId[] {
  return ROLE_PANELS[data.user.role].filter((panel) => data[PANEL_SECTION[panel]] !== null);
}
