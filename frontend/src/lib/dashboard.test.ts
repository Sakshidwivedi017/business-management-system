import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ApiError, type ApiRequestOptions } from "./api.ts";
import { createAuthorizedFetch, type Fetcher } from "./auth.ts";
import {
  INITIAL_DASHBOARD_STATE,
  ROLE_DESCRIPTIONS,
  dashboardKpis,
  dashboardPanels,
  firstName,
  formatAmount,
  formatCount,
  greeting,
  loadDashboard,
  parseDashboard,
  poStatusLabel,
} from "./dashboard.ts";
import { ROLE_LABELS } from "./roles.ts";
import { createSession } from "./session.ts";
import type { DashboardResponse, InventoryOverview, ProcurementOverview, Role } from "./types";

const INVENTORY: InventoryOverview = {
  total_items: 501,
  active_items: 500,
  active_locations: 5,
  low_stock: { count: 149, fallback_threshold: "0" },
};
const PROCUREMENT: ProcurementOverview = {
  total_vendors: 34,
  active_vendors: 33,
  purchase_orders_by_status: [
    { status: "placed", currency: "INR", purchase_orders: 32, total_amount: "4437362.15" },
    { status: "placed", currency: "USD", purchase_orders: 1, total_amount: "542.10" },
  ],
  open_purchase_orders: [
    { currency: "INR", purchase_orders: 35, total_amount: "4788746.73", lines_pending_delivery: 66 },
    { currency: "USD", purchase_orders: 1, total_amount: "542.10", lines_pending_delivery: 1 },
  ],
};
const ANALYTICS = {
  stock_by_location: [{ location_name: "132-1", stocked_items: 344, items_in_stock: 266, items_out_of_stock: 78 }],
  recent_transactions: { days: 30, by_type: [{ transaction_type: "inbound", transactions: 10 }] },
  top_vendors_by_spend: [{ vendor_name: "Sterling Steels", currency: "INR", purchase_orders: 2, total_amount: "3021209.47" }],
};

/** Responses as the backend sends them for each role. */
const RESPONSES: Record<Role, DashboardResponse> = {
  inventory_manager: {
    user: { full_name: "Asha Rao", role: "inventory_manager" },
    inventory: INVENTORY,
    procurement: null,
    analytics: null,
  },
  procurement_manager: {
    user: { full_name: "Vikram Shah", role: "procurement_manager" },
    inventory: INVENTORY,
    procurement: PROCUREMENT,
    analytics: null,
  },
  owner: {
    user: { full_name: "Meera Iyer", role: "owner" },
    inventory: INVENTORY,
    procurement: PROCUREMENT,
    analytics: ANALYTICS,
  },
};

function fetcherReturning(result: () => unknown) {
  const calls: { path: string; options?: ApiRequestOptions }[] = [];
  const fetcher = (async (path: string, options?: ApiRequestOptions) => {
    calls.push({ path, options });
    return result();
  }) as Fetcher;
  return { fetcher, calls };
}

describe("loadDashboard", () => {
  it("starts in the loading state", () => {
    assert.deepEqual(INITIAL_DASHBOARD_STATE, { status: "loading" });
  });

  it("calls GET /api/dashboard and returns the parsed data", async () => {
    const { fetcher, calls } = fetcherReturning(() => RESPONSES.owner);
    assert.deepEqual(await loadDashboard(fetcher), { status: "ready", data: RESPONSES.owner });
    assert.equal(calls[0].path, "/api/dashboard");
    assert.equal(calls[0].options?.method, undefined); // GET, no body
    assert.equal(calls[0].options?.body, undefined);
  });

  it("turns API errors into a safe error state, and a retry loads again", async () => {
    let fail = true;
    const { fetcher } = fetcherReturning(() => {
      if (fail) throw new ApiError(503, "The dashboard is temporarily unavailable; please try again");
      return RESPONSES.inventory_manager;
    });
    assert.deepEqual(await loadDashboard(fetcher), {
      status: "error",
      message: "The dashboard is temporarily unavailable; please try again",
    });
    fail = false;
    assert.equal((await loadDashboard(fetcher)).status, "ready");
  });

  it("never shows a non-API error's text", async () => {
    const { fetcher } = fetcherReturning(() => {
      throw new TypeError("Cannot read properties of undefined (reading 'x')");
    });
    assert.deepEqual(await loadDashboard(fetcher), { status: "error", message: "Unable to load the dashboard. Please try again." });
  });

  it("rejects an unusable response", async () => {
    const { fetcher } = fetcherReturning(() => ({ unexpected: true }));
    assert.equal((await loadDashboard(fetcher)).status, "error");
  });

  it("rethrows an aborted load", async () => {
    const controller = new AbortController();
    const { fetcher } = fetcherReturning(() => {
      controller.abort();
      throw new DOMException("aborted", "AbortError");
    });
    await assert.rejects(loadDashboard(fetcher, controller.signal), { name: "AbortError" });
  });

  it("ends the session through authFetch on a 401", async () => {
    const session = createSession("jwt-abc", 1800, Date.now());
    const ended: [string, string][] = [];
    const { fetcher: backend, calls } = fetcherReturning(() => {
      throw new ApiError(401, "Could not validate credentials");
    });
    const authFetch = createAuthorizedFetch(() => session, (reason, token) => ended.push([reason, token]), Date.now, backend);

    const state = await loadDashboard(authFetch);
    assert.equal(state.status, "error");
    assert.deepEqual(ended, [["revoked", "jwt-abc"]]);
    assert.equal(calls[0].options?.token, "jwt-abc");
  });
});

describe("dashboardKpis", () => {
  const ids = (role: Role) => dashboardKpis(RESPONSES[role]).map((kpi) => kpi.id);

  it("shows only inventory KPIs to an inventory manager", () => {
    assert.deepEqual(ids("inventory_manager"), ["active_items", "low_stock", "locations"]);
  });

  it("adds procurement KPIs for a procurement manager and the owner", () => {
    const all = ["active_items", "low_stock", "locations", "open_purchase_orders", "vendors"];
    assert.deepEqual(ids("procurement_manager"), all);
    assert.deepEqual(ids("owner"), all);
  });

  it("renders the backend numbers without inventing any", () => {
    const kpis = Object.fromEntries(dashboardKpis(RESPONSES.owner).map((kpi) => [kpi.id, kpi]));
    assert.equal(kpis.active_items.value, "500");
    assert.equal(kpis.active_items.detail, "501 items in the catalogue");
    assert.equal(kpis.low_stock.value, "149");
    assert.equal(kpis.low_stock.tone, "warning");
    assert.equal(kpis.locations.value, "5");
    assert.equal(kpis.open_purchase_orders.value, "36"); // a count of orders, summed; money is not
    assert.equal(kpis.vendors.value, "33");
  });

  it("keeps open value per currency instead of summing currencies", () => {
    const detail = dashboardKpis(RESPONSES.owner).find((kpi) => kpi.id === "open_purchase_orders")?.detail ?? "";
    assert.match(detail, /47,88,746\.73/);
    assert.match(detail, /542\.10/);
    assert.match(detail, /·/);
  });

  it("follows the response, not the role: missing sections show nothing", () => {
    const stripped: DashboardResponse = { ...RESPONSES.owner, procurement: null, analytics: null };
    assert.ok(!dashboardKpis(stripped).some((kpi) => kpi.id === "vendors" || kpi.id === "open_purchase_orders"));
    assert.deepEqual(dashboardKpis({ ...stripped, inventory: null }), []);
  });
});

describe("dashboardPanels", () => {
  it("gives each role its own panel", () => {
    assert.deepEqual(dashboardPanels(RESPONSES.inventory_manager), ["inventory_operations"]);
    assert.deepEqual(dashboardPanels(RESPONSES.procurement_manager), ["procurement_overview"]);
    assert.deepEqual(dashboardPanels(RESPONSES.owner), ["business_overview", "procurement_overview"]);
  });

  it("never renders a panel whose data the backend did not send", () => {
    assert.deepEqual(dashboardPanels({ ...RESPONSES.owner, analytics: null, procurement: null }), []);
    assert.deepEqual(dashboardPanels({ ...RESPONSES.inventory_manager, inventory: null }), []);
  });
});

describe("parseDashboard", () => {
  it("accepts every role's response unchanged", () => {
    for (const response of Object.values(RESPONSES)) assert.deepEqual(parseDashboard(response), response);
  });

  it("rejects responses without a valid user", () => {
    for (const body of [null, [], {}, { user: null }, { user: { full_name: "X", role: "admin" } }]) {
      assert.equal(parseDashboard(body), null);
    }
  });

  it("drops malformed sections and rows instead of failing", () => {
    const parsed = parseDashboard({
      user: { full_name: "Meera Iyer", role: "owner" },
      inventory: { total_items: "501" },
      procurement: { ...PROCUREMENT, open_purchase_orders: [{ currency: "INR" }, PROCUREMENT.open_purchase_orders[1]] },
      analytics: { stock_by_location: "nope" },
    });
    assert.ok(parsed);
    assert.equal(parsed.inventory, null);
    assert.deepEqual(parsed.procurement?.open_purchase_orders, [PROCUREMENT.open_purchase_orders[1]]);
    assert.deepEqual(parsed.analytics, {
      stock_by_location: [],
      recent_transactions: { days: 0, by_type: [] },
      top_vendors_by_spend: [],
    });
  });

  it("treats missing sections as not granted", () => {
    const parsed = parseDashboard({ user: { full_name: "Asha Rao", role: "inventory_manager" } });
    assert.deepEqual(parsed, { user: { full_name: "Asha Rao", role: "inventory_manager" }, inventory: null, procurement: null, analytics: null });
  });
});

describe("header", () => {
  it("greets by time of day and first name", () => {
    assert.equal(greeting(new Date(2026, 9, 7, 9)), "Good morning");
    assert.equal(greeting(new Date(2026, 9, 7, 14)), "Good afternoon");
    assert.equal(greeting(new Date(2026, 9, 7, 19)), "Good evening");
    assert.equal(firstName("  Asha  Rao "), "Asha");
  });

  it("has a readable label and description for every role", () => {
    for (const role of Object.keys(ROLE_LABELS) as Role[]) {
      assert.ok(ROLE_LABELS[role] && !ROLE_LABELS[role].includes("_"));
      assert.ok(ROLE_DESCRIPTIONS[role]);
    }
  });
});

describe("formatting", () => {
  it("formats counts and amounts in their own currency", () => {
    assert.equal(formatCount(4788746), "47,88,746");
    assert.equal(formatAmount("4788746.73", "INR"), "₹47,88,746.73");
    assert.match(formatAmount("542.10", "USD"), /^(US)?\$542\.10$/);
    assert.equal(formatAmount(null, "INR"), "—");
    assert.equal(formatAmount("12.5", "XYZ1"), "XYZ1 12.5");
  });

  it("labels purchase order statuses", () => {
    assert.equal(poStatusLabel("partial"), "Partially received");
    assert.equal(poStatusLabel("on_hold"), "On hold");
  });
});
