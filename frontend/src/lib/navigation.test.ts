import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { NAV_ITEMS, isActivePath } from "./navigation.ts";

describe("isActivePath", () => {
  it("matches the route itself and its sub-pages", () => {
    assert.ok(isActivePath("/dashboard", "/dashboard"));
    assert.ok(isActivePath("/chat/123", "/chat"));
  });

  it("does not match routes that only share a prefix", () => {
    assert.ok(!isActivePath("/chatter", "/chat"));
    assert.ok(!isActivePath("/", "/dashboard"));
  });
});

describe("NAV_ITEMS", () => {
  it("lists the Phase 5 routes once each", () => {
    assert.deepEqual(NAV_ITEMS.map((item) => item.href), ["/dashboard", "/chat"]);
  });
});
