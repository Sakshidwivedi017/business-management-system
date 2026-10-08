/**
 * Frontend mirrors of the backend API contracts.
 *
 * Each type matches a Pydantic model in backend/app (named next to it). The backend is the
 * source of truth: change these only when the backend contract changes.
 */

// --- health (app/api/health.py) ---------------------------------------------------------

export type HealthResponse = {
  status: string;
  app: string;
  environment: string;
};

// --- auth (app/auth/permissions.py, app/api/auth.py) -------------------------------------

/** Role. Display and UX only: the backend decides what each role may do. */
export type Role = "inventory_manager" | "procurement_manager" | "owner";

/** AuthenticatedUser: the caller's identity, always read from the current users row. */
export type AuthenticatedUser = {
  id: string; // UUID
  email: string;
  full_name: string;
  role: Role;
};

/** LoginRequest */
export type LoginRequest = {
  email: string;
  password: string;
};

/** LoginResponse. There is no refresh token: a new login is needed when the token expires. */
export type LoginResponse = {
  access_token: string;
  token_type: "bearer";
  expires_in: number; // seconds
  user: AuthenticatedUser;
};

// --- dashboard (app/api/dashboard.py) -------------------------------------------------------

/** A decimal amount as the backend serializes it (exact, as a string). Always paired with a currency. */
export type DecimalString = string;

export type LowStock = {
  count: number; // stock rows (an item at a location) at or below their minimum level
  fallback_threshold: DecimalString; // the minimum used where a row has none
};

export type InventoryOverview = {
  total_items: number;
  active_items: number;
  active_locations: number;
  low_stock: LowStock;
};

export type PurchaseOrdersByStatus = {
  status: string;
  currency: string;
  purchase_orders: number;
  total_amount: DecimalString | null;
};

export type OpenPurchaseOrders = {
  currency: string;
  purchase_orders: number;
  total_amount: DecimalString | null;
  lines_pending_delivery: number;
};

export type ProcurementOverview = {
  total_vendors: number;
  active_vendors: number;
  purchase_orders_by_status: PurchaseOrdersByStatus[];
  open_purchase_orders: OpenPurchaseOrders[];
};

export type LocationStock = {
  location_name: string;
  stocked_items: number;
  items_in_stock: number;
  items_out_of_stock: number;
};

export type TransactionTypeCount = {
  transaction_type: string;
  transactions: number;
};

export type VendorSpend = {
  vendor_name: string;
  currency: string;
  purchase_orders: number;
  total_amount: DecimalString | null;
};

export type BusinessAnalytics = {
  stock_by_location: LocationStock[];
  recent_transactions: { days: number; by_type: TransactionTypeCount[] };
  top_vendors_by_spend: VendorSpend[];
};

/** DashboardResponse: each section is null when the user's permissions do not cover it. */
export type DashboardResponse = {
  user: { full_name: string; role: Role };
  inventory: InventoryOverview | null; // inventory:read
  procurement: ProcurementOverview | null; // procurement:read
  analytics: BusinessAnalytics | null; // analytics:read (owner)
};

// --- response plan (app/agent/planning.py) ------------------------------------------------

export type Intent =
  | "informational"
  | "analytical"
  | "knowledge"
  | "mixed"
  | "operational"
  | "conversational"
  | "unclear";

export type Source = "structured_tool" | "semantic_retrieval" | "mixed" | "none" | "clarification_required";

export type Presentation = "text" | "table" | "chart" | "summary" | "mixed";

export type Confidence = "high" | "medium" | "low";

export type DataKind = "records" | "categorical" | "time_series";

export type ChartKind = "bar" | "line";

/** Dataset: points at rows inside one tool result. It carries metadata only, never the rows. */
export type Dataset = {
  tool: string;
  tool_call_id: string;
  path: string; // dotted key inside the tool's result; "" when the result itself is the list
  kind: DataKind;
  rows: number; // row count
  x: string | null;
  y: string[];
  series: string | null; // split rows by this field, never sum across it (e.g. currency)
  chart: ChartKind | null; // set on the datasets the plan suggests charting
};

/** ResponsePlan: how the backend suggests presenting the answer. Advice only, never authorization. */
export type ResponsePlan = {
  intent: Intent;
  source: Source;
  presentation: Presentation;
  requested_presentation: Presentation | null;
  confidence: Confidence;
  clarification_required: boolean;
  missing: string[];
  reason: string;
  datasets: Dataset[];
};

// --- mutation safety (app/agent/mutations.py) ----------------------------------------------

export type MutationState =
  | "confirmation_required"
  | "executed"
  | "failed"
  | "cancelled"
  | "expired"
  | "already_executed"
  | "not_pending";

/** The two mutation tools; null when the backend could not tell which operation a step belonged to. */
export type MutationOperation = "record_stock_movement" | "create_purchase_order";

/** MutationStatus: what happened to a proposed change. `message` is written by the backend, never the model. */
export type MutationStatus = {
  proposal_id: string | null;
  operation: MutationOperation | null;
  state: MutationState;
  message: string;
  entity_id: string | null;
  details: Record<string, unknown>;
};

// --- chat data (app/agent/datasets.py) --------------------------------------------------------

export type ColumnKind = "text" | "number" | "amount" | "date" | "boolean";

/** One table cell. Decimals arrive as exact strings ("102.50"). */
export type Cell = string | number | boolean | null;

export type DatasetColumn = {
  key: string;
  label: string;
  kind: ColumnKind; // "amount": money in the same row's `currency`
};

/** A planned dataset's rows, copied from this turn's own tool result and bounded by the backend. */
export type DatasetPayload = {
  tool: string;
  title: string;
  kind: DataKind;
  x: string | null;
  y: string[];
  series: string | null; // never combine rows across this field (e.g. currency)
  chart: ChartKind | null; // set when the plan suggests charting this dataset
  columns: DatasetColumn[];
  rows: Record<string, Cell>[];
  total_rows: number;
  truncated: boolean; // fewer rows were sent than the tool returned
};

/** A headline figure from a summary tool. */
export type Metric = {
  label: string;
  value: number | string;
};

export type ChatData = {
  datasets: DatasetPayload[];
  metrics: Metric[];
};

// --- chat (app/api/chat.py) -----------------------------------------------------------------

/** ChatRequest. Identity and role come from the access token, never the body. */
export type ChatRequest = {
  conversation_id: string; // UUID
  message: string;
};

/** ChatResponse: one agent turn. */
export type ChatResponse = {
  conversation_id: string;
  message: string; // the assistant's reply
  plan: ResponsePlan;
  mutation: MutationStatus | null; // "confirmation_required" means a change awaits confirm/cancel
  data: ChatData; // empty for text answers and change steps
};
