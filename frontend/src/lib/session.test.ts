import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  SESSION_KEY,
  clearSession,
  createSession,
  isExpired,
  parseSession,
  readSession,
  writeSession,
} from "./session.ts";
import { MemoryStore } from "./test-utils.ts";

const NOW = 1_700_000_000_000;

describe("createSession / isExpired", () => {
  it("computes expiresAt from expires_in seconds", () => {
    assert.deepEqual(createSession("tok", 1800, NOW), { token: "tok", expiresAt: NOW + 1_800_000 });
  });

  it("treats the expiry instant itself as expired", () => {
    const session = createSession("tok", 60, NOW);
    assert.equal(isExpired(session, NOW + 59_999), false);
    assert.equal(isExpired(session, NOW + 60_000), true);
  });

  it("treats a zero lifetime as already expired", () => {
    assert.equal(isExpired(createSession("tok", 0, NOW), NOW), true);
  });
});

describe("parseSession", () => {
  it("accepts the stored shape", () => {
    assert.deepEqual(parseSession(JSON.stringify({ token: "t", expiresAt: 5 })), { token: "t", expiresAt: 5 });
  });

  it("rejects missing and malformed values", () => {
    for (const raw of [null, "", "not json", "null", "[]", '"t"', '{"token":"t"}', '{"token":"","expiresAt":1}',
      '{"token":1,"expiresAt":1}', '{"token":"t","expiresAt":"1"}']) {
      assert.equal(parseSession(raw), null, String(raw));
    }
  });
});

describe("readSession", () => {
  it("returns a valid session", () => {
    const store = new MemoryStore();
    const session = createSession("tok", 1800, NOW);
    writeSession(store, session);
    assert.deepEqual(readSession(store, NOW + 1000), { kind: "valid", session });
  });

  it("rejects and removes an expired session", () => {
    const store = new MemoryStore();
    writeSession(store, createSession("tok", 60, NOW));
    assert.deepEqual(readSession(store, NOW + 60_000), { kind: "expired" });
    assert.equal(store.getItem(SESSION_KEY), null);
  });

  it("rejects and removes a malformed session", () => {
    const store = new MemoryStore();
    store.setItem(SESSION_KEY, "{broken");
    assert.deepEqual(readSession(store, NOW), { kind: "none" });
    assert.equal(store.getItem(SESSION_KEY), null);
  });

  it("is signed out without storage or a stored session", () => {
    assert.deepEqual(readSession(null, NOW), { kind: "none" });
    assert.deepEqual(readSession(new MemoryStore(), NOW), { kind: "none" });
  });
});

describe("clearSession", () => {
  it("removes every app key and leaves other keys alone", () => {
    const store = new MemoryStore();
    writeSession(store, createSession("tok", 1800, NOW));
    store.setItem("lecxe.chat.messages", "[]");
    store.setItem("other-app", "keep");
    clearSession(store);
    assert.deepEqual(store.keys(), ["other-app"]);
  });

  it("only stores the token and expiry", () => {
    const store = new MemoryStore();
    writeSession(store, createSession("tok", 1800, NOW));
    assert.deepEqual(Object.keys(JSON.parse(store.getItem(SESSION_KEY) ?? "{}")).sort(), ["expiresAt", "token"]);
  });
});
