/**
 * Chat with the assistant (POST /api/chat): conversation state for the current tab.
 *
 * The backend keeps the conversation (checkpointed per user and conversation id); this
 * module only keeps what the page shows. Proposed changes are never executed here: Confirm
 * and Cancel send "confirm" / "cancel" as ordinary messages, and only the backend's
 * MutationStatus in the reply says what happened.
 *
 * State lives in memory, in a store owned by the signed-in part of the app, so sign-out or a
 * different user discards it; nothing is written to storage.
 */

import { ApiError } from "./api.ts";
import type { Fetcher } from "./auth.ts";
import type { ChatData, ChatRequest, ChatResponse, MutationState, MutationStatus, ResponsePlan } from "./types";
import { parseChatData } from "./visualize.ts";

/** The backend's limit (app/agent/graph.py MAX_MESSAGE_LENGTH), applied to the trimmed message. */
export const MAX_MESSAGE_LENGTH = 4000;
/** How long the backend keeps a proposal confirmable (app/agent/mutations.py PROPOSAL_TTL_SECONDS). */
export const PROPOSAL_TTL_MS = 30 * 60 * 1000;

export const CONFIRM_MESSAGE = "confirm";
export const CANCEL_MESSAGE = "cancel";

/** Starter questions every role can ask: they need only inventory and catalogue access, which all roles have. */
export const SUGGESTED_PROMPTS = [
  "Show me low-stock items",
  "Which locations hold inventory?",
  "Show the most recent stock movements",
  "What can you help me with?",
] as const;

// --- messages and state ----------------------------------------------------------------------

export type UserMessage = {
  id: string;
  role: "user";
  content: string;
  status: "sending" | "sent" | "failed";
  /** A safe, user-facing reason when status is "failed". */
  error: string | null;
};

export type AssistantMessage = {
  id: string;
  role: "assistant";
  content: string;
  /** How the backend suggests presenting the answer, and the rows behind it (Layer 14). */
  plan: ResponsePlan;
  data: ChatData;
  mutation: MutationStatus | null;
  /** Local receipt time (ms), used only to stop offering an expired confirmation. Never displayed. */
  receivedAt: number;
};

export type ChatMessage = UserMessage | AssistantMessage;

export type ChatState = {
  conversationId: string;
  messages: ChatMessage[];
  /** The user message whose request is in flight; at most one at a time. */
  pendingId: string | null;
};

export type ChatAction =
  | { type: "send"; id: string; content: string }
  | { type: "retry"; id: string }
  | { type: "received"; conversationId: string; requestId: string; assistantId: string; response: ChatResponse; receivedAt: number }
  | { type: "failed"; conversationId: string; requestId: string; error: string }
  | { type: "reset"; conversationId: string };

export function initialChatState(conversationId: string): ChatState {
  return { conversationId, messages: [], pendingId: null };
}

function updateUser(messages: ChatMessage[], id: string, patch: Partial<UserMessage>): ChatMessage[] {
  return messages.map((m) => (m.id === id && m.role === "user" ? { ...m, ...patch } : m));
}

/** A failed message can be retried only while it is the latest message and nothing is in flight. */
export function canRetry(state: ChatState, id: string): boolean {
  const last = state.messages[state.messages.length - 1];
  return state.pendingId === null && last?.id === id && last.role === "user" && last.status === "failed";
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "send":
      if (state.pendingId !== null) return state;
      return {
        ...state,
        pendingId: action.id,
        messages: [...state.messages, { id: action.id, role: "user", content: action.content, status: "sending", error: null }],
      };
    case "retry":
      if (!canRetry(state, action.id)) return state;
      return { ...state, pendingId: action.id, messages: updateUser(state.messages, action.id, { status: "sending", error: null }) };
    case "received": {
      // A reply for an abandoned conversation or a request no longer in flight is dropped.
      if (action.conversationId !== state.conversationId || action.requestId !== state.pendingId) return state;
      const reply: AssistantMessage = {
        id: action.assistantId,
        role: "assistant",
        content: action.response.message,
        plan: action.response.plan,
        mutation: action.response.mutation,
        data: action.response.data,
        receivedAt: action.receivedAt,
      };
      return {
        ...state,
        pendingId: null,
        messages: [...updateUser(state.messages, action.requestId, { status: "sent", error: null }), reply],
      };
    }
    case "failed":
      if (action.conversationId !== state.conversationId || action.requestId !== state.pendingId) return state;
      return {
        ...state,
        pendingId: null,
        messages: updateUser(state.messages, action.requestId, { status: "failed", error: action.error }),
      };
    case "reset":
      return initialChatState(action.conversationId);
  }
}

// --- input -----------------------------------------------------------------------------------

export type MessageCheck = { ok: true; message: string } | { ok: false; reason: "empty" | "too_long" };

/** Trim and check a message before it is sent. */
export function checkMessage(text: string): MessageCheck {
  const message = text.trim();
  if (!message) return { ok: false, reason: "empty" };
  if (message.length > MAX_MESSAGE_LENGTH) return { ok: false, reason: "too_long" };
  return { ok: true, message };
}

// --- API -------------------------------------------------------------------------------------

const MUTATION_STATES: readonly MutationState[] = [
  "confirmation_required", "executed", "failed", "cancelled", "expired", "already_executed", "not_pending",
];

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseMutation(value: unknown): MutationStatus | null {
  if (!isObject(value)) return null;
  const { proposal_id, operation, state, message, entity_id, details } = value;
  if (typeof state !== "string" || !(MUTATION_STATES as readonly string[]).includes(state)) return null;
  if (typeof message !== "string") return null;
  return {
    proposal_id: typeof proposal_id === "string" ? proposal_id : null,
    operation: operation === "record_stock_movement" || operation === "create_purchase_order" ? operation : null,
    state: state as MutationState,
    message,
    entity_id: typeof entity_id === "string" ? entity_id : null,
    details: isObject(details) ? details : {},
  };
}

/** The reply in the expected shape; throws an ApiError for anything the page cannot show. */
export function parseChatResponse(body: unknown): ChatResponse {
  if (!isObject(body) || typeof body.message !== "string" || typeof body.conversation_id !== "string" || !isObject(body.plan)) {
    throw new ApiError(200, "The assistant returned an unexpected response. Please try again.");
  }
  return {
    conversation_id: body.conversation_id,
    message: body.message,
    plan: body.plan as ResponsePlan,
    // Malformed datasets are dropped here; the reply text is never held back by them.
    data: parseChatData(body.data),
    mutation: parseMutation(body.mutation),
  };
}

/** POST /api/chat through the session's authFetch (which ends the session on a 401). */
export async function postChatMessage(authFetch: Fetcher, conversationId: string, message: string): Promise<ChatResponse> {
  const body: ChatRequest = { conversation_id: conversationId, message };
  return parseChatResponse(await authFetch<unknown>("/api/chat", { method: "POST", body }));
}

const BUSY_PREFIX = "Another message in this conversation is still being answered";

/** A safe message for a failed send. Backend `detail` strings are written for users; others are not shown. */
export function chatErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong while sending your message. Please try again.";
  if (error.status === 401) return "Your session has ended. Please sign in again.";
  if (error.status === 409 && error.message.startsWith(BUSY_PREFIX)) {
    return "That conversation is still processing. Please wait a moment and try again.";
  }
  if (error.isNetworkError) return "Unable to reach the assistant. Check your connection and try again.";
  return error.message;
}

// --- conversation ids ---------------------------------------------------------------------------

/** A random v4 UUID; crypto.randomUUID needs a secure context, so fall back to getRandomValues. */
export function newConversationId(): string {
  const c = globalThis.crypto;
  if (typeof c.randomUUID === "function") return c.randomUUID();
  const bytes = c.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// --- confirmations -------------------------------------------------------------------------------

/**
 * Whether Confirm/Cancel are offered for this assistant message. Mirrors the backend rule that
 * only the very next user message can answer a proposal, plus its 30-minute lifetime. The
 * backend still decides: a stale click only gets its "nothing to confirm" reply.
 */
export function isConfirmable(state: ChatState, messageId: string, now: number): boolean {
  const last = state.messages[state.messages.length - 1];
  return (
    state.pendingId === null &&
    last?.id === messageId &&
    last.role === "assistant" &&
    last.mutation?.state === "confirmation_required" &&
    now - last.receivedAt < PROPOSAL_TTL_MS
  );
}

/** Why a proposal awaiting confirmation is no longer confirmable here. */
export function proposalClosedReason(state: ChatState, messageId: string, now: number): "answered" | "expired" | null {
  const index = state.messages.findIndex((m) => m.id === messageId);
  const message = state.messages[index];
  if (!message || message.role !== "assistant" || message.mutation?.state !== "confirmation_required") return null;
  if (index < state.messages.length - 1) return "answered";
  if (now - message.receivedAt >= PROPOSAL_TTL_MS) return "expired";
  return null;
}

// --- store ---------------------------------------------------------------------------------------

export type SendOutcome = "sent" | "failed" | "rejected";

export type ChatStore = {
  getState(): ChatState;
  subscribe(listener: () => void): () => void;
  /** Send a new message. Resolves once the reply (or failure) is in the state. */
  send(text: string): Promise<SendOutcome>;
  /** Resend a failed message, as is, in the same conversation. */
  retry(messageId: string): Promise<SendOutcome>;
  /** Answer the proposal in this assistant message, through the normal chat flow. */
  answerProposal(messageId: string, decision: "confirm" | "cancel"): Promise<SendOutcome>;
  /** Start a new conversation locally; the backend is not called. */
  reset(): void;
};

export function createChatStore({
  fetcher,
  newId = newConversationId,
  now = Date.now,
}: {
  fetcher: Fetcher;
  newId?: () => string;
  now?: () => number;
}): ChatStore {
  let state = initialChatState(newId());
  const listeners = new Set<() => void>();

  function dispatch(action: ChatAction) {
    const next = chatReducer(state, action);
    if (next === state) return false;
    state = next;
    listeners.forEach((listener) => listener());
    return true;
  }

  async function request(requestId: string, content: string): Promise<SendOutcome> {
    const conversationId = state.conversationId;
    try {
      const response = await postChatMessage(fetcher, conversationId, content);
      dispatch({ type: "received", conversationId, requestId, assistantId: newId(), response, receivedAt: now() });
      return "sent";
    } catch (err) {
      dispatch({ type: "failed", conversationId, requestId, error: chatErrorMessage(err) });
      return "failed";
    }
  }

  function send(text: string): Promise<SendOutcome> {
    const check = checkMessage(text);
    if (!check.ok) return Promise.resolve("rejected");
    const id = newId();
    if (!dispatch({ type: "send", id, content: check.message })) return Promise.resolve("rejected");
    return request(id, check.message);
  }

  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    send,
    retry(messageId) {
      const message = state.messages.find((m) => m.id === messageId);
      if (!message || !dispatch({ type: "retry", id: messageId })) return Promise.resolve("rejected");
      return request(messageId, message.content);
    },
    answerProposal(messageId, decision) {
      if (!isConfirmable(state, messageId, now())) return Promise.resolve("rejected");
      return send(decision === "confirm" ? CONFIRM_MESSAGE : CANCEL_MESSAGE);
    },
    reset() {
      dispatch({ type: "reset", conversationId: newId() });
    },
  };
}

// --- presenting a mutation -------------------------------------------------------------------------

export type MutationTone = "warning" | "success" | "neutral" | "danger";

export const OPERATION_LABELS: Record<NonNullable<MutationStatus["operation"]>, string> = {
  record_stock_movement: "Stock movement",
  create_purchase_order: "Purchase order",
};

export const MUTATION_HEADINGS: Record<MutationState, { heading: string; tone: MutationTone }> = {
  confirmation_required: { heading: "Confirmation required", tone: "warning" },
  executed: { heading: "Change completed", tone: "success" },
  already_executed: { heading: "Already completed", tone: "success" },
  cancelled: { heading: "Cancelled", tone: "neutral" },
  expired: { heading: "Expired", tone: "neutral" },
  failed: { heading: "Change not made", tone: "danger" },
  not_pending: { heading: "Nothing to confirm", tone: "neutral" },
};

export type Fact = { label: string; value: string };
export type MutationLine = { item: string; quantity: string; price: string; total: string };

export type MutationView = {
  operation: string | null;
  heading: string;
  tone: MutationTone;
  facts: Fact[];
  lines: MutationLine[];
};

function text(details: Record<string, unknown>, key: string): string | null {
  const value = details[key];
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

function pushFact(facts: Fact[], label: string, value: string | null) {
  if (value !== null) facts.push({ label, value });
}

function movementFacts(d: Record<string, unknown>): Fact[] {
  const facts: Fact[] = [];
  const unit = text(d, "unit") ?? "";
  const direction = text(d, "direction");
  pushFact(facts, "Action", direction === "inbound" ? "Add stock" : direction === "outbound" ? "Remove stock" : null);
  const code = text(d, "item_code");
  const name = text(d, "item_name");
  pushFact(facts, "Item", code && name ? `${code} — ${name}` : (code ?? name));
  pushFact(facts, "Location", text(d, "location"));
  const quantity = text(d, "quantity");
  pushFact(facts, "Quantity", quantity && `${quantity} ${unit}`.trim());
  const before = text(d, "quantity_before");
  const after = text(d, "quantity_after");
  pushFact(facts, "Stock level", before && after ? `${before} → ${after} ${unit}`.trim() : null);
  return facts;
}

function orderFacts(d: Record<string, unknown>): Fact[] {
  const facts: Fact[] = [];
  const currency = text(d, "currency");
  const money = (key: string) => {
    const value = text(d, key);
    return value && currency ? `${currency} ${value}` : value;
  };
  pushFact(facts, "PO number", text(d, "po_number"));
  pushFact(facts, "Vendor", text(d, "vendor_name"));
  pushFact(facts, "Subtotal", money("subtotal"));
  pushFact(facts, "Tax", money("tax_amount"));
  pushFact(facts, "Total", money("total_amount"));
  return facts;
}

function orderLines(d: Record<string, unknown>): MutationLine[] {
  if (!Array.isArray(d.lines)) return [];
  return d.lines.filter(isObject).map((line) => {
    const unit = text(line, "unit") ?? "";
    const code = text(line, "item_code");
    const name = text(line, "item_name");
    const tax = text(line, "tax_percentage");
    return {
      item: code && name ? `${code} — ${name}` : (code ?? name ?? "Item"),
      quantity: `${text(line, "quantity") ?? "?"} ${unit}`.trim(),
      price: `${text(line, "unit_price") ?? "?"}${tax ? ` + ${tax}% tax` : ""}`,
      total: text(line, "line_total") ?? "?",
    };
  });
}

/** What the card shows: only known detail fields, as text. Internal ids are never shown. */
export function describeMutation(mutation: MutationStatus): MutationView {
  const { heading, tone } = MUTATION_HEADINGS[mutation.state];
  const details = mutation.details;
  return {
    operation: mutation.operation ? OPERATION_LABELS[mutation.operation] : null,
    heading,
    tone,
    facts:
      mutation.operation === "record_stock_movement"
        ? movementFacts(details)
        : mutation.operation === "create_purchase_order"
          ? orderFacts(details)
          : [],
    lines: mutation.operation === "create_purchase_order" ? orderLines(details) : [],
  };
}
