import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ApiError, type ApiRequestOptions } from "./api.ts";
import { createAuthorizedFetch, type Fetcher } from "./auth.ts";
import {
  MAX_MESSAGE_LENGTH,
  MUTATION_HEADINGS,
  PROPOSAL_TTL_MS,
  SUGGESTED_PROMPTS,
  canRetry,
  chatErrorMessage,
  checkMessage,
  createChatStore,
  describeMutation,
  isConfirmable,
  newConversationId,
  parseChatResponse,
  proposalClosedReason,
  type AssistantMessage,
  type ChatStore,
  type UserMessage,
} from "./chat.ts";
import { createSession, type StoredSession } from "./session.ts";
import { EMPTY_CHAT_DATA } from "./visualize.ts";
import type { ChatResponse, MutationState, MutationStatus, ResponsePlan } from "./types";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const PLAN: ResponsePlan = {
  intent: "informational",
  source: "structured_tool",
  presentation: "table",
  requested_presentation: null,
  confidence: "high",
  clarification_required: false,
  missing: [],
  reason: "asks about specific business records",
  datasets: [
    { tool: "get_low_stock_items", tool_call_id: "call_1", path: "items", kind: "records", rows: 12, x: null, y: [], series: null, chart: null },
  ],
};
const OPERATIONAL_PLAN: ResponsePlan = { ...PLAN, intent: "operational", presentation: "text", datasets: [] };

// Shaped like app/agent/mutations.py: a preview has no transaction_id (assigned on execution).
const MOVEMENT_PREVIEW = {
  item_code: "BO-MC-0100",
  item_name: "Hex bolt M10",
  unit: "pcs",
  location: "132-1",
  direction: "inbound",
  quantity: "20",
  quantity_before: "10",
  quantity_after: "30",
};
const PROPOSAL: MutationStatus = {
  proposal_id: "6f1d1c51-9a9e-4c0e-8d3c-0b9a7c3e2f10",
  operation: "record_stock_movement",
  state: "confirmation_required",
  message:
    "Please confirm: add 20 pcs of BO-MC-0100 (Hex bolt M10) at 132-1. Stock there would go from 10 to 30 pcs." +
    '\n\nReply "confirm" to go ahead or "cancel" to discard it. Nothing has been changed yet.',
  entity_id: null,
  details: MOVEMENT_PREVIEW,
};

function reply(message: string, extra: Partial<ChatResponse> = {}): (conversationId: string) => ChatResponse {
  return (conversation_id) => ({ conversation_id, message, plan: PLAN, mutation: null, data: EMPTY_CHAT_DATA, ...extra });
}

function status(state: MutationState, message: string, extra: Partial<MutationStatus> = {}): MutationStatus {
  return { ...PROPOSAL, state, message, ...extra };
}

/** A fake backend: each call takes the next scripted outcome; calls can be held open. */
function fakeBackend(outcomes: (((conversationId: string) => unknown) | Error)[]) {
  const calls: { path: string; options?: ApiRequestOptions }[] = [];
  const gates: (() => void)[] = [];
  let hold = false;
  const fetcher = (async (path: string, options?: ApiRequestOptions) => {
    calls.push({ path, options });
    if (hold) await new Promise<void>((resolve) => gates.push(resolve));
    const outcome = outcomes.shift();
    if (!outcome) throw new Error("no scripted outcome");
    if (outcome instanceof Error) throw outcome;
    return outcome((options?.body as { conversation_id: string }).conversation_id);
  }) as Fetcher;
  return {
    fetcher,
    calls,
    holdRequests: () => (hold = true),
    release: () => {
      hold = false;
      gates.splice(0).forEach((open) => open());
    },
  };
}

let idCounter = 0;
const ids = () => `00000000-0000-4000-8000-${String(++idCounter).padStart(12, "0")}`;

function makeStore(outcomes: Parameters<typeof fakeBackend>[0], now = () => 1_000) {
  const backend = fakeBackend(outcomes);
  const store = createChatStore({ fetcher: backend.fetcher, newId: ids, now });
  return { store, ...backend };
}

const users = (store: ChatStore) => store.getState().messages.filter((m): m is UserMessage => m.role === "user");
const assistants = (store: ChatStore) =>
  store.getState().messages.filter((m): m is AssistantMessage => m.role === "assistant");

// --- conversation identity --------------------------------------------------------------------

describe("conversation id", () => {
  it("is a random v4 UUID, different each time", () => {
    const a = newConversationId();
    const b = newConversationId();
    assert.match(a, UUID);
    assert.notEqual(a, b);
  });

  it("is created with the store and reused for every message", async () => {
    const { store, calls } = makeStore([reply("first"), reply("second")]);
    const conversationId = store.getState().conversationId;
    await store.send("Show me low-stock items");
    await store.send("What about 132-1?");
    assert.deepEqual(
      calls.map((c) => (c.options?.body as { conversation_id: string }).conversation_id),
      [conversationId, conversationId],
    );
  });
});

// --- input ------------------------------------------------------------------------------------

describe("checkMessage", () => {
  it("rejects empty and whitespace-only messages", () => {
    assert.deepEqual(checkMessage(""), { ok: false, reason: "empty" });
    assert.deepEqual(checkMessage("  \n\t "), { ok: false, reason: "empty" });
  });

  it("trims whitespace", () => {
    assert.deepEqual(checkMessage("  low stock?\n"), { ok: true, message: "low stock?" });
  });

  it("accepts exactly 4000 characters and rejects 4001", () => {
    assert.equal(MAX_MESSAGE_LENGTH, 4000);
    assert.equal(checkMessage("x".repeat(4000)).ok, true);
    assert.deepEqual(checkMessage("x".repeat(4001)), { ok: false, reason: "too_long" });
    assert.equal(checkMessage(`  ${"x".repeat(4000)}  `).ok, true); // the limit applies after trimming
  });

  it("does not send rejected messages", async () => {
    const { store, calls } = makeStore([]);
    assert.equal(await store.send("   "), "rejected");
    assert.equal(await store.send("x".repeat(4001)), "rejected");
    assert.equal(calls.length, 0);
    assert.deepEqual(store.getState().messages, []);
  });
});

// --- sending ------------------------------------------------------------------------------------

describe("sending", () => {
  it("posts {conversation_id, message} to /api/chat with the trimmed text", async () => {
    const { store, calls } = makeStore([reply("ok")]);
    await store.send("  Show me low-stock items  ");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, "/api/chat");
    assert.equal(calls[0].options?.method, "POST");
    assert.deepEqual(calls[0].options?.body, {
      conversation_id: store.getState().conversationId,
      message: "Show me low-stock items",
    });
  });

  it("shows the user message and a pending state at once, then the reply", async () => {
    const { store, holdRequests, release } = makeStore([reply("12 items are low on stock.")]);
    holdRequests();
    const done = store.send("Show me low-stock items");

    const pending = store.getState();
    assert.equal(pending.messages.length, 1);
    assert.deepEqual(pending.messages[0], {
      id: pending.pendingId,
      role: "user",
      content: "Show me low-stock items",
      status: "sending",
      error: null,
    });

    release();
    assert.equal(await done, "sent");
    const state = store.getState();
    assert.equal(state.pendingId, null);
    assert.equal(users(store)[0].status, "sent");
    const [answer] = assistants(store);
    assert.equal(answer.content, "12 items are low on stock.");
    assert.deepEqual(answer.plan, PLAN); // kept for Layer 14
    assert.equal(answer.mutation, null);
    assert.deepEqual(state.messages.map((m) => m.role), ["user", "assistant"]);
  });

  it("refuses a second message while one is in flight", async () => {
    const { store, calls, holdRequests, release } = makeStore([reply("first")]);
    holdRequests();
    const first = store.send("one");
    assert.equal(await store.send("two"), "rejected");
    release();
    await first;
    assert.equal(calls.length, 1);
    assert.deepEqual(store.getState().messages.map((m) => m.content), ["one", "first"]);
  });

  it("notifies subscribers on every change", async () => {
    const { store } = makeStore([reply("ok")]);
    let notified = 0;
    const unsubscribe = store.subscribe(() => notified++);
    await store.send("hi");
    unsubscribe();
    assert.equal(notified, 2); // sent, then received
  });

  it("sends through authFetch, which ends the session on a 401", async () => {
    const session = createSession("jwt-abc", 1800, Date.now());
    const ended: string[] = [];
    const backend = fakeBackend([new ApiError(401, "Could not validate credentials")]);
    const authFetch = createAuthorizedFetch(() => session, (reason) => ended.push(reason), Date.now, backend.fetcher);
    const store = createChatStore({ fetcher: authFetch, newId: ids });

    assert.equal(await store.send("hi"), "failed");
    assert.deepEqual(ended, ["revoked"]);
    assert.equal(backend.calls[0].options?.token, "jwt-abc");
    assert.equal(users(store)[0].error, "Your session has ended. Please sign in again.");
  });

  it("a 401 mid-conversation ends the session, keeps the history and never reuses the token", async () => {
    // Like AuthProvider: ending the session drops it, so later requests have no token to send.
    let session: StoredSession | null = createSession("jwt-abc", 1800, Date.now());
    const ended: string[] = [];
    const backend = fakeBackend([
      reply(PROPOSAL.message, { plan: OPERATIONAL_PLAN, mutation: PROPOSAL }),
      new ApiError(401, "Could not validate credentials: token_version mismatch"),
    ]);
    const authFetch = createAuthorizedFetch(() => session, (reason) => {
      ended.push(reason);
      session = null;
    }, Date.now, backend.fetcher);
    const store = createChatStore({ fetcher: authFetch, newId: ids });
    await store.send("Add 20 pcs of BO-MC-0100 at 132-1");

    assert.equal(await store.answerProposal(assistants(store)[0].id, "confirm"), "failed");
    assert.deepEqual(ended, ["revoked"]);
    const failed = users(store)[1];
    assert.deepEqual([failed.content, failed.status, failed.error],
      ["confirm", "failed", "Your session has ended. Please sign in again."]);
    assert.equal(assistants(store).length, 1); // no reply, and the proposal is not shown as done
    assert.equal(assistants(store)[0].mutation?.state, "confirmation_required");

    // A retry is refused before reaching the backend, and the message is not duplicated.
    assert.equal(await store.retry(failed.id), "failed");
    assert.equal(backend.calls.length, 2);
    assert.deepEqual(store.getState().messages.map((m) => m.role), ["user", "assistant", "user"]);
    assert.equal(store.getState().pendingId, null);
  });
});

// --- errors and retry -------------------------------------------------------------------------------

describe("errors", () => {
  const busy = new ApiError(409, "Another message in this conversation is still being answered; please wait for it to finish");

  it("maps failures to safe messages", () => {
    assert.equal(chatErrorMessage(busy), "That conversation is still processing. Please wait a moment and try again.");
    assert.equal(chatErrorMessage(new ApiError(409, "Not enough stock at 132-1")), "Not enough stock at 132-1");
    assert.equal(chatErrorMessage(new ApiError(503, "The assistant is temporarily unavailable; please try again")),
      "The assistant is temporarily unavailable; please try again");
    assert.match(chatErrorMessage(new ApiError(0, "x")), /Unable to reach the assistant/);
    assert.match(chatErrorMessage(new TypeError("undefined is not a function")), /Something went wrong/);
  });

  it("keeps the user message, marks it failed and does not retry on its own", async () => {
    const { store, calls } = makeStore([busy]);
    assert.equal(await store.send("hello"), "failed");
    assert.equal(calls.length, 1);
    const [message] = users(store);
    assert.equal(message.status, "failed");
    assert.equal(message.error, "That conversation is still processing. Please wait a moment and try again.");
    assert.equal(store.getState().pendingId, null);
  });

  it("shows server and network failures safely", async () => {
    const { store } = makeStore([new ApiError(500, "Something went wrong while answering; please try again"), new ApiError(0, "")]);
    await store.send("one");
    assert.equal(users(store)[0].error, "Something went wrong while answering; please try again");
    await store.send("two");
    assert.match(users(store)[1].error ?? "", /Unable to reach the assistant/);
  });

  it("rejects an unusable reply", () => {
    assert.throws(() => parseChatResponse({ message: 1 }), ApiError);
    assert.throws(() => parseChatResponse(null), ApiError);
  });

  it("retries the same message in the same conversation without duplicating it", async () => {
    const { store, calls } = makeStore([new ApiError(503, "unavailable"), reply("Here you go.")]);
    await store.send("Show me low-stock items");
    const failed = users(store)[0];
    assert.ok(canRetry(store.getState(), failed.id));

    assert.equal(await store.retry(failed.id), "sent");
    assert.deepEqual(calls[1].options?.body, calls[0].options?.body);
    assert.deepEqual(store.getState().messages.map((m) => [m.role, m.content]), [
      ["user", "Show me low-stock items"],
      ["assistant", "Here you go."],
    ]);
    assert.equal(users(store)[0].status, "sent");
  });

  it("only offers retry for the latest message", async () => {
    const { store } = makeStore([new ApiError(503, "unavailable"), reply("ok")]);
    await store.send("first");
    const failed = users(store)[0];
    await store.send("second");
    assert.equal(canRetry(store.getState(), failed.id), false);
    assert.equal(await store.retry(failed.id), "rejected");
  });
});

// --- new conversation ---------------------------------------------------------------------------------

describe("new conversation", () => {
  it("starts a fresh conversation locally, without calling the backend", async () => {
    const { store, calls } = makeStore([reply("ok")]);
    const before = store.getState().conversationId;
    await store.send("hi");
    store.reset();
    const state = store.getState();
    assert.notEqual(state.conversationId, before);
    assert.match(state.conversationId, UUID);
    assert.deepEqual(state.messages, []);
    assert.equal(state.pendingId, null);
    assert.equal(calls.length, 1);
  });

  it("drops a reply that arrives for the abandoned conversation", async () => {
    const { store, holdRequests, release } = makeStore([reply("late reply")]);
    holdRequests();
    const done = store.send("hi");
    store.reset();
    release();
    await done;
    assert.deepEqual(store.getState().messages, []);
    assert.equal(store.getState().pendingId, null);
  });
});

// --- mutations ----------------------------------------------------------------------------------------

describe("mutation proposals", () => {
  it("parses a proposal from the reply", () => {
    const parsed = parseChatResponse({ conversation_id: "c", message: PROPOSAL.message, plan: OPERATIONAL_PLAN, mutation: PROPOSAL });
    assert.deepEqual(parsed.mutation, PROPOSAL);
  });

  it("ignores a malformed or unknown mutation instead of offering confirmation", () => {
    for (const mutation of [{ state: "executed" }, { ...PROPOSAL, state: "done" }, "x"]) {
      const parsed = parseChatResponse({ conversation_id: "c", message: "m", plan: PLAN, mutation });
      assert.equal(parsed.mutation, null);
    }
  });

  async function proposed(outcomes: Parameters<typeof fakeBackend>[0] = [], now = () => 1_000) {
    const setup = makeStore([reply(PROPOSAL.message, { plan: OPERATIONAL_PLAN, mutation: PROPOSAL }), ...outcomes], now);
    await setup.store.send("Receive 20 pcs of BO-MC-0100 at 132-1");
    return { ...setup, proposalId: assistants(setup.store)[0].id };
  }

  it("is confirmable only while it is the latest message and nothing is in flight", async () => {
    const { store, proposalId } = await proposed();
    assert.equal(isConfirmable(store.getState(), proposalId, 1_000), true);
    assert.equal(proposalClosedReason(store.getState(), proposalId, 1_000), null);
  });

  it("confirm sends 'confirm' in the same conversation; only the reply decides the outcome", async () => {
    const executed = status("executed", "Done: added 20 pcs of BO-MC-0100 (Hex bolt M10) at 132-1.", {
      entity_id: "tx-1",
      details: { ...MOVEMENT_PREVIEW, transaction_id: "tx-1" },
    });
    const { store, calls, proposalId, holdRequests, release } = await proposed([reply(executed.message, { mutation: executed })]);

    holdRequests();
    const done = store.answerProposal(proposalId, "confirm");
    // While in flight the proposal is not confirmable again and nothing is reported as done.
    assert.equal(isConfirmable(store.getState(), proposalId, 1_000), false);
    assert.equal(assistants(store).length, 1);
    release();
    await done;

    assert.deepEqual(calls[1].options?.body, { conversation_id: store.getState().conversationId, message: "confirm" });
    assert.equal(calls[1].options?.body && (calls[1].options.body as { conversation_id: string }).conversation_id,
      (calls[0].options?.body as { conversation_id: string }).conversation_id);
    const outcome = assistants(store)[1];
    assert.equal(outcome.mutation?.state, "executed");
    assert.equal(isConfirmable(store.getState(), proposalId, 1_000), false);
    assert.equal(proposalClosedReason(store.getState(), proposalId, 1_000), "answered");
  });

  it("cancel sends 'cancel' in the same conversation", async () => {
    const cancelled = status("cancelled", "Cancelled. Nothing was changed.", { details: {} });
    const { store, calls, proposalId } = await proposed([reply(cancelled.message, { mutation: cancelled })]);
    await store.answerProposal(proposalId, "cancel");
    assert.deepEqual(calls[1].options?.body, { conversation_id: store.getState().conversationId, message: "cancel" });
    assert.equal(assistants(store)[1].mutation?.state, "cancelled");
  });

  it("a failed confirmation is not reported as a change", async () => {
    const failed = status("failed", "The change was not made: You do not have permission to make this change", { details: {} });
    const { store, proposalId } = await proposed([reply(failed.message, { mutation: failed })]);
    await store.answerProposal(proposalId, "confirm");
    const outcome = assistants(store)[1];
    assert.equal(outcome.mutation?.state, "failed");
    assert.equal(describeMutation(outcome.mutation!).heading, "Change not made");
    assert.equal(isConfirmable(store.getState(), outcome.id, 1_000), false);
  });

  it("a network failure on confirm leaves the click retryable, not the card", async () => {
    const { store, calls, proposalId } = await proposed([new ApiError(0, ""), reply("Done.", { mutation: status("executed", "Done.") })]);
    assert.equal(await store.answerProposal(proposalId, "confirm"), "failed");
    const confirmMessage = users(store)[1];
    assert.equal(confirmMessage.content, "confirm");
    assert.equal(isConfirmable(store.getState(), proposalId, 1_000), false);
    assert.equal(await store.retry(confirmMessage.id), "sent");
    assert.equal((calls[2].options?.body as { message: string }).message, "confirm");
  });

  it("an intervening message ends the confirmation", async () => {
    const { store, calls, proposalId } = await proposed([reply("Here are the locations.")]);
    await store.send("Which locations hold inventory?");
    assert.equal(isConfirmable(store.getState(), proposalId, 1_000), false);
    assert.equal(proposalClosedReason(store.getState(), proposalId, 1_000), "answered");
    assert.equal(await store.answerProposal(proposalId, "confirm"), "rejected");
    assert.equal(calls.length, 2);
  });

  it("is not confirmable after 30 minutes", async () => {
    let clock = 1_000;
    const { store, calls, proposalId } = await proposed([], () => clock);
    const later = 1_000 + PROPOSAL_TTL_MS;
    assert.equal(isConfirmable(store.getState(), proposalId, later - 1), true);
    assert.equal(isConfirmable(store.getState(), proposalId, later), false);
    assert.equal(proposalClosedReason(store.getState(), proposalId, later), "expired");
    clock = later;
    assert.equal(await store.answerProposal(proposalId, "confirm"), "rejected");
    assert.equal(calls.length, 1);
  });

  it("finished states never offer confirmation", async () => {
    for (const state of ["executed", "failed", "cancelled", "expired", "already_executed", "not_pending"] as const) {
      const { store } = makeStore([reply("m", { mutation: status(state, "m") })]);
      await store.send("confirm");
      assert.equal(isConfirmable(store.getState(), assistants(store)[0].id, 1_000), false, state);
    }
  });
});

describe("describeMutation", () => {
  it("shows a stock movement preview in plain terms, without ids", () => {
    const view = describeMutation(PROPOSAL);
    assert.equal(view.operation, "Stock movement");
    assert.equal(view.heading, "Confirmation required");
    assert.equal(view.tone, "warning");
    assert.deepEqual(view.facts, [
      { label: "Action", value: "Add stock" },
      { label: "Item", value: "BO-MC-0100 — Hex bolt M10" },
      { label: "Location", value: "132-1" },
      { label: "Quantity", value: "20 pcs" },
      { label: "Stock level", value: "10 → 30 pcs" },
    ]);
    const executed = describeMutation(status("executed", "Done", { details: { ...MOVEMENT_PREVIEW, transaction_id: "tx-1" } }));
    assert.ok(!JSON.stringify(executed).includes("tx-1"));
  });

  it("shows a purchase order with its lines and totals in its currency", () => {
    const view = describeMutation({
      ...PROPOSAL,
      operation: "create_purchase_order",
      details: {
        vendor_id: "v-1",
        vendor_name: "Sterling Steels Pvt. Ltd.",
        currency: "INR",
        subtotal: "1000",
        tax_amount: "180",
        total_amount: "1180",
        lines: [{ item_code: "RM-MT-0012", item_name: "MS plate", quantity: "10", unit: "kg", unit_price: "100",
                  tax_percentage: "18", line_total: "1180" }],
      },
    });
    assert.equal(view.operation, "Purchase order");
    assert.deepEqual(view.facts, [
      { label: "Vendor", value: "Sterling Steels Pvt. Ltd." },
      { label: "Subtotal", value: "INR 1000" },
      { label: "Tax", value: "INR 180" },
      { label: "Total", value: "INR 1180" },
    ]);
    assert.deepEqual(view.lines, [{ item: "RM-MT-0012 — MS plate", quantity: "10 kg", price: "100 + 18% tax", total: "1180" }]);
    assert.ok(!JSON.stringify(view).includes("v-1"));
  });

  it("has a heading for every backend state, and ignores unknown detail shapes", () => {
    assert.deepEqual(Object.keys(MUTATION_HEADINGS).sort(), [
      "already_executed", "cancelled", "confirmation_required", "executed", "expired", "failed", "not_pending",
    ]);
    const view = describeMutation(status("not_pending", "There is no pending change to confirm.", { operation: null, details: { x: { y: 1 } } }));
    assert.deepEqual(view, { operation: null, heading: "Nothing to confirm", tone: "neutral", facts: [], lines: [] });
  });
});

describe("suggested prompts", () => {
  it("only asks about inventory and the catalogue, which every role can read", () => {
    for (const prompt of SUGGESTED_PROMPTS) {
      assert.doesNotMatch(prompt, /purchase order|vendor|supplier|spend|procurement/i);
    }
  });
});
