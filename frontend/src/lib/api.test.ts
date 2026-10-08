import assert from "node:assert/strict";
import { afterEach, describe, it, mock } from "node:test";

import { API_BASE_URL, ApiError, apiFetch, apiUrl, buildRequestInit, defaultMessageForStatus, toApiError } from "./api.ts";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("apiUrl", () => {
  it("joins the base URL and an absolute API path", () => {
    assert.equal(apiUrl("/api/health"), `${API_BASE_URL}/api/health`);
  });

  it("uses the local backend when NEXT_PUBLIC_API_BASE_URL is not set", () => {
    assert.equal(API_BASE_URL, "http://localhost:8000");
  });

  it("rejects relative paths", () => {
    assert.throws(() => apiUrl("api/health"), /must start with "\/"/);
  });
});

describe("buildRequestInit", () => {
  it("defaults to GET without a body or token", () => {
    const init = buildRequestInit({});
    assert.equal(init.method, "GET");
    assert.equal(init.body, undefined);
    assert.deepEqual(init.headers, { Accept: "application/json" });
    assert.equal(init.cache, "no-store");
  });

  it("serializes a JSON body and defaults to POST", () => {
    const init = buildRequestInit({ body: { email: "a@b.co", password: "x" } });
    assert.equal(init.method, "POST");
    assert.equal(init.body, '{"email":"a@b.co","password":"x"}');
    assert.equal((init.headers as Record<string, string>)["Content-Type"], "application/json");
  });

  it("attaches the Bearer token only when given", () => {
    assert.equal((buildRequestInit({ token: "abc" }).headers as Record<string, string>).Authorization, "Bearer abc");
    assert.equal((buildRequestInit({ token: null }).headers as Record<string, string>).Authorization, undefined);
  });

  it("keeps an explicit method", () => {
    assert.equal(buildRequestInit({ method: "DELETE" }).method, "DELETE");
  });
});

describe("toApiError", () => {
  it("uses a string detail as the message", () => {
    const error = toApiError(401, { detail: "Invalid email or password" });
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 401);
    assert.equal(error.message, "Invalid email or password");
    assert.ok(error.isUnauthorized);
    assert.deepEqual(error.fieldErrors, []);
  });

  it("turns a FastAPI validation array into field errors", () => {
    const error = toApiError(422, {
      detail: [
        { loc: ["body", "email"], msg: "String should have at least 3 characters", type: "string_too_short" },
        { loc: ["body", "lines", 0, "quantity"], msg: "Input should be greater than 0", type: "greater_than" },
        { loc: ["body"], msg: "Field required", type: "missing" },
        { nonsense: true },
      ],
    });
    assert.equal(error.status, 422);
    assert.equal(error.message, "Some of the information provided is not valid.");
    assert.deepEqual(error.fieldErrors, [
      { field: "email", message: "String should have at least 3 characters" },
      { field: "lines.0.quantity", message: "Input should be greater than 0" },
      { field: "body", message: "Field required" },
    ]);
  });

  it("falls back to a generic message for unknown bodies", () => {
    assert.equal(toApiError(503, undefined).message, defaultMessageForStatus(503));
    assert.equal(toApiError(500, { error: "Traceback ..." }).message, defaultMessageForStatus(500));
    assert.equal(toApiError(404, { detail: { nested: "object" } }).message, defaultMessageForStatus(404));
    assert.equal(toApiError(400, { detail: "   " }).message, defaultMessageForStatus(400));
  });

  it("does not show an overly long detail", () => {
    assert.equal(toApiError(500, { detail: "x".repeat(1000) }).message, defaultMessageForStatus(500));
  });
});

describe("defaultMessageForStatus", () => {
  it("covers known statuses, server errors and the rest", () => {
    assert.match(defaultMessageForStatus(401), /sign in again/);
    assert.match(defaultMessageForStatus(502), /server/);
    assert.equal(defaultMessageForStatus(418), "The request could not be completed.");
  });
});

describe("apiFetch", () => {
  afterEach(() => mock.restoreAll());

  it("returns the parsed JSON body", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => jsonResponse(200, { status: "ok" }));
    const body = await apiFetch<{ status: string }>("/api/health", { token: "t0k" });
    assert.deepEqual(body, { status: "ok" });
    const [url, init] = fetchMock.mock.calls[0].arguments as [string, RequestInit];
    assert.equal(url, `${API_BASE_URL}/api/health`);
    assert.equal((init.headers as Record<string, string>).Authorization, "Bearer t0k");
  });

  it("throws an ApiError built from the error body", async () => {
    mock.method(globalThis, "fetch", async () => jsonResponse(409, { detail: "Another message is being answered" }));
    await assert.rejects(apiFetch("/api/chat", { body: {} }), (err: unknown) => {
      assert.ok(err instanceof ApiError);
      assert.equal(err.status, 409);
      assert.equal(err.message, "Another message is being answered");
      return true;
    });
  });

  it("uses a generic message when an error body is not JSON", async () => {
    mock.method(globalThis, "fetch", async () => new Response("Internal Server Error", { status: 500 }));
    await assert.rejects(apiFetch("/api/health"), { status: 500, message: defaultMessageForStatus(500) });
  });

  it("throws an ApiError when a success body is not JSON", async () => {
    mock.method(globalThis, "fetch", async () => new Response("<html></html>", { status: 200 }));
    await assert.rejects(apiFetch("/api/health"), { name: "ApiError", status: 200 });
  });

  it("reports an unreachable server as status 0", async () => {
    mock.method(globalThis, "fetch", async () => {
      throw new TypeError("fetch failed");
    });
    await assert.rejects(apiFetch("/api/health"), (err: unknown) => {
      assert.ok(err instanceof ApiError);
      assert.ok(err.isNetworkError);
      assert.match(err.message, /Unable to reach the server/);
      return true;
    });
  });

  it("rethrows an abort untouched", async () => {
    const controller = new AbortController();
    mock.method(globalThis, "fetch", async () => {
      controller.abort();
      throw new DOMException("The operation was aborted.", "AbortError");
    });
    await assert.rejects(apiFetch("/api/health", { signal: controller.signal }), { name: "AbortError" });
  });
});
