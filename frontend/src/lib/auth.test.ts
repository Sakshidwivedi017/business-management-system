import assert from "node:assert/strict";
import { afterEach, describe, it, mock } from "node:test";

import { API_BASE_URL, ApiError, type ApiRequestOptions } from "./api.ts";
import {
  SESSION_END_MESSAGES,
  createAuthorizedFetch,
  endSession,
  getCurrentUser,
  login,
  loginErrorMessage,
  loginFieldErrors,
  parseAuthenticatedUser,
  restoreSession,
  startSession,
  validateLogin,
  type Fetcher,
} from "./auth.ts";
import { ROLE_LABELS, isRole } from "./roles.ts";
import { SESSION_KEY, createSession, writeSession } from "./session.ts";
import { MemoryStore } from "./test-utils.ts";
import type { AuthenticatedUser, LoginResponse } from "./types";

const NOW = 1_700_000_000_000;
const USER: AuthenticatedUser = {
  id: "8f1c2a8e-1111-4b7e-9c55-000000000001",
  email: "inventory@example.com",
  full_name: "Asha Rao",
  role: "inventory_manager",
};
const LOGIN_RESPONSE: LoginResponse = { access_token: "jwt-abc", token_type: "bearer", expires_in: 1800, user: USER };

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** A Fetcher returning fixed values, recording each call. */
function fakeFetcher(result: () => unknown) {
  const calls: { path: string; options?: ApiRequestOptions }[] = [];
  const fetcher = (async (path: string, options?: ApiRequestOptions) => {
    calls.push({ path, options });
    return result();
  }) as Fetcher;
  return { fetcher, calls };
}

afterEach(() => mock.restoreAll());

describe("login", () => {
  it("posts trimmed email and password as JSON to /api/auth/login", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => jsonResponse(200, LOGIN_RESPONSE));
    const res = await login({ email: "  inventory@example.com ", password: " secret " });
    assert.deepEqual(res, LOGIN_RESPONSE);
    const [url, init] = fetchMock.mock.calls[0].arguments as [string, RequestInit];
    assert.equal(url, `${API_BASE_URL}/api/auth/login`);
    assert.equal(init.method, "POST");
    assert.deepEqual(JSON.parse(String(init.body)), { email: "inventory@example.com", password: " secret " });
    assert.equal((init.headers as Record<string, string>).Authorization, undefined);
  });

  it("rejects a response without a token or with an unknown role", async () => {
    await assert.rejects(login({ email: "a@b.co", password: "x" }, fakeFetcher(() => ({ ...LOGIN_RESPONSE, access_token: "" })).fetcher), ApiError);
    await assert.rejects(
      login({ email: "a@b.co", password: "x" }, fakeFetcher(() => ({ ...LOGIN_RESPONSE, user: { ...USER, role: "admin" } })).fetcher),
      ApiError,
    );
  });

  it("surfaces a 401 as an ApiError", async () => {
    mock.method(globalThis, "fetch", async () => jsonResponse(401, { detail: "Invalid email or password" }));
    await assert.rejects(login({ email: "a@b.co", password: "wrong" }), { name: "ApiError", status: 401 });
  });
});

describe("getCurrentUser", () => {
  it("sends the Bearer token to /api/auth/me and parses the user", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => jsonResponse(200, { ...USER, extra: "ignored" }));
    assert.deepEqual(await getCurrentUser("jwt-abc"), USER);
    const [url, init] = fetchMock.mock.calls[0].arguments as [string, RequestInit];
    assert.equal(url, `${API_BASE_URL}/api/auth/me`);
    assert.equal((init.headers as Record<string, string>).Authorization, "Bearer jwt-abc");
  });

  it("rejects a malformed user", () => {
    assert.throws(() => parseAuthenticatedUser({ ...USER, full_name: undefined }), ApiError);
    assert.throws(() => parseAuthenticatedUser(null), ApiError);
  });
});

describe("validateLogin", () => {
  it("requires a plausible email and a password", () => {
    assert.deepEqual(validateLogin({ email: "", password: "" }), {
      email: "Enter your email address.",
      password: "Enter your password.",
    });
    assert.deepEqual(validateLogin({ email: "not-an-email", password: "x" }), { email: "Enter a valid email address." });
    assert.deepEqual(validateLogin({ email: " a@b.co ", password: "x" }), {});
    assert.equal(validateLogin({ email: "a@b.co", password: "x".repeat(257) }).password, "Password is too long.");
  });
});

describe("login errors", () => {
  it("maps statuses to safe messages", () => {
    assert.equal(loginErrorMessage(new ApiError(401, "Invalid email or password")), "Invalid email or password.");
    assert.match(loginErrorMessage(new ApiError(0, "x")), /Unable to connect/);
    assert.match(loginErrorMessage(new ApiError(503, "x")), /Unable to connect/);
    assert.match(loginErrorMessage(new ApiError(500, "Traceback")), /Unable to connect/);
    assert.equal(loginErrorMessage(new ApiError(429, "Too many requests.")), "Too many requests.");
    assert.equal(loginErrorMessage(new Error("boom")), "Sign-in failed. Please try again.");
  });

  it("keeps only the form's field errors from a 422", () => {
    const error = new ApiError(422, "invalid", [
      { field: "email", message: "too short" },
      { field: "other", message: "ignored" },
    ]);
    assert.equal(loginErrorMessage(error), "Please check the highlighted fields.");
    assert.deepEqual(loginFieldErrors(error), { email: "too short" });
    assert.deepEqual(loginFieldErrors(new Error("x")), {});
  });
});

describe("restoreSession", () => {
  it("is unauthenticated when nothing is stored, without calling the backend", async () => {
    const fetchUser = mock.fn(async () => USER);
    assert.deepEqual(await restoreSession(new MemoryStore(), NOW, fetchUser), { status: "unauthenticated", reason: null });
    assert.equal(fetchUser.mock.callCount(), 0);
  });

  it("does not accept an expired token and reports the expiry", async () => {
    const store = new MemoryStore();
    writeSession(store, createSession("old", 60, NOW - 120_000));
    const fetchUser = mock.fn(async () => USER);
    assert.deepEqual(await restoreSession(store, NOW, fetchUser), { status: "unauthenticated", reason: "expired" });
    assert.equal(fetchUser.mock.callCount(), 0);
    assert.equal(store.getItem(SESSION_KEY), null);
  });

  it("authenticates a valid token once /me confirms it", async () => {
    const store = new MemoryStore();
    const session = createSession("jwt-abc", 1800, NOW);
    writeSession(store, session);
    const fetchUser = mock.fn(async (_token: string) => USER);
    assert.deepEqual(await restoreSession(store, NOW + 1000, fetchUser), { status: "authenticated", session, user: USER });
    assert.equal(fetchUser.mock.calls[0].arguments[0], "jwt-abc");
  });

  it("clears a revoked token when /me answers 401", async () => {
    const store = new MemoryStore();
    writeSession(store, createSession("revoked", 1800, NOW));
    const state = await restoreSession(store, NOW, async () => {
      throw new ApiError(401, "Could not validate credentials");
    });
    assert.deepEqual(state, { status: "unauthenticated", reason: "revoked" });
    assert.equal(store.getItem(SESSION_KEY), null);
  });

  it("keeps the token for a retry when the backend is unreachable", async () => {
    const store = new MemoryStore();
    writeSession(store, createSession("jwt-abc", 1800, NOW));
    const state = await restoreSession(store, NOW, async () => {
      throw new ApiError(0, "Unable to reach the server.");
    });
    assert.deepEqual(state, { status: "error", message: "Unable to load your session." });
    assert.notEqual(store.getItem(SESSION_KEY), null);
  });

  it("rethrows an aborted check", async () => {
    const store = new MemoryStore();
    writeSession(store, createSession("jwt-abc", 1800, NOW));
    await assert.rejects(
      restoreSession(store, NOW, async () => {
        throw new DOMException("aborted", "AbortError");
      }),
      { name: "AbortError" },
    );
  });
});

describe("startSession / endSession", () => {
  it("stores only token and expiry, replacing any earlier tab state", () => {
    const store = new MemoryStore();
    store.setItem("lecxe.chat.messages", "[]");
    const state = startSession(store, LOGIN_RESPONSE, NOW);
    assert.deepEqual(state, { status: "authenticated", session: { token: "jwt-abc", expiresAt: NOW + 1_800_000 }, user: USER });
    assert.deepEqual(store.keys(), [SESSION_KEY]);
  });

  it("logout clears the session", () => {
    const store = new MemoryStore();
    startSession(store, LOGIN_RESPONSE, NOW);
    assert.deepEqual(endSession(store, null), { status: "unauthenticated", reason: null });
    assert.deepEqual(store.keys(), []);
  });
});

describe("createAuthorizedFetch", () => {
  const session = createSession("jwt-abc", 1800, NOW);

  it("adds the session's Bearer token", async () => {
    const { fetcher, calls } = fakeFetcher(() => ({ ok: true }));
    const authFetch = createAuthorizedFetch(() => session, () => assert.fail("must not end"), () => NOW, fetcher);
    assert.deepEqual(await authFetch("/api/chat", { body: { message: "hi" } }), { ok: true });
    assert.deepEqual(calls[0], { path: "/api/chat", options: { body: { message: "hi" }, token: "jwt-abc" } });
  });

  it("ends the session on a 401 and still rejects", async () => {
    const ended: [string, string][] = [];
    const { fetcher } = fakeFetcher(() => {
      throw new ApiError(401, "Could not validate credentials");
    });
    const authFetch = createAuthorizedFetch(() => session, (reason, token) => ended.push([reason, token]), () => NOW, fetcher);
    await assert.rejects(authFetch("/api/auth/me"), { status: 401 });
    assert.deepEqual(ended, [["revoked", "jwt-abc"]]);
  });

  it("does not end the session on other errors", async () => {
    const { fetcher } = fakeFetcher(() => {
      throw new ApiError(503, "unavailable");
    });
    const authFetch = createAuthorizedFetch(() => session, () => assert.fail("must not end"), () => NOW, fetcher);
    await assert.rejects(authFetch("/api/chat"), { status: 503 });
  });

  it("ends an expired session without calling the backend", async () => {
    const ended: string[] = [];
    const { fetcher, calls } = fakeFetcher(() => ({}));
    const authFetch = createAuthorizedFetch(() => session, (reason) => ended.push(reason), () => session.expiresAt, fetcher);
    await assert.rejects(authFetch("/api/chat"), { status: 401, message: SESSION_END_MESSAGES.expired });
    assert.deepEqual(ended, ["expired"]);
    assert.equal(calls.length, 0);
  });

  it("refuses to send a request without a session", async () => {
    const { fetcher, calls } = fakeFetcher(() => ({}));
    const authFetch = createAuthorizedFetch(() => null, () => assert.fail("nothing to end"), () => NOW, fetcher);
    await assert.rejects(authFetch("/api/chat"), { status: 401 });
    assert.equal(calls.length, 0);
  });
});

describe("roles", () => {
  it("labels every backend role and recognises only those", () => {
    assert.deepEqual(ROLE_LABELS, {
      inventory_manager: "Inventory Manager",
      procurement_manager: "Procurement Manager",
      owner: "Owner",
    });
    assert.ok(isRole("owner"));
    assert.ok(!isRole("admin"));
    assert.ok(!isRole(undefined));
  });
});
