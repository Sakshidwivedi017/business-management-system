/**
 * Typed client for the FastAPI backend.
 *
 * Every request goes through apiFetch, which builds the URL, attaches an optional Bearer
 * token, parses JSON and turns any failure into an ApiError carrying a message that is safe
 * to show. The client holds no session: callers pass the token they want used.
 */

import type { HealthResponse } from "./types";

export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(/\/+$/, "");

/** Longest backend message shown as-is; anything longer is replaced by the generic one. */
const MAX_MESSAGE_LENGTH = 300;

const NETWORK_MESSAGE = "Unable to reach the server. Check your connection and try again.";
const UNEXPECTED_RESPONSE_MESSAGE = "The server returned an unexpected response. Please try again.";
const INVALID_INPUT_MESSAGE = "Some of the information provided is not valid.";

const STATUS_MESSAGES: Record<number, string> = {
  400: "The request could not be processed.",
  401: "Your session is no longer valid. Please sign in again.",
  403: "You do not have permission to do this.",
  404: "The requested information was not found.",
  409: "This could not be completed because of a conflict. Please try again.",
  422: INVALID_INPUT_MESSAGE,
  429: "Too many requests. Please wait a moment and try again.",
  503: "The service is temporarily unavailable. Please try again shortly.",
};

/** One invalid input field from a FastAPI validation error. */
export type FieldError = {
  field: string;
  message: string;
};

export class ApiError extends Error {
  /** HTTP status, or 0 when the server could not be reached. */
  readonly status: number;
  readonly fieldErrors: FieldError[];

  constructor(status: number, message: string, fieldErrors: FieldError[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fieldErrors = fieldErrors;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isNetworkError(): boolean {
    return this.status === 0;
  }
}

export function defaultMessageForStatus(status: number): string {
  if (STATUS_MESSAGES[status]) return STATUS_MESSAGES[status];
  if (status >= 500) return "Something went wrong on the server. Please try again.";
  return "The request could not be completed.";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** FastAPI's validation detail: [{loc: ["body", "email"], msg: "...", type: "..."}, ...]. */
function toFieldErrors(detail: unknown[]): FieldError[] {
  const errors: FieldError[] = [];
  for (const entry of detail) {
    if (!isRecord(entry) || typeof entry.msg !== "string") continue;
    const loc = Array.isArray(entry.loc) ? entry.loc : [];
    // Drop the location prefix ("body", "query", ...) so only the field path remains.
    const path = loc.slice(loc.length > 1 ? 1 : 0).filter((part) => typeof part === "string" || typeof part === "number");
    errors.push({ field: path.join(".") || "request", message: entry.msg });
  }
  return errors;
}

/**
 * Build an ApiError from an error response body. Only the backend's `detail` is read:
 * a short string is shown as-is (the backend writes these for users), a validation array
 * becomes field errors, and anything else falls back to a generic message for the status.
 */
export function toApiError(status: number, body: unknown): ApiError {
  const detail = isRecord(body) ? body.detail : undefined;
  if (typeof detail === "string") {
    const message = detail.trim();
    if (message && message.length <= MAX_MESSAGE_LENGTH) return new ApiError(status, message);
  }
  if (Array.isArray(detail)) {
    return new ApiError(status, INVALID_INPUT_MESSAGE, toFieldErrors(detail));
  }
  return new ApiError(status, defaultMessageForStatus(status));
}

/** Absolute backend URL for an API path such as "/api/health". */
export function apiUrl(path: string): string {
  if (!path.startsWith("/")) {
    throw new Error(`API path must start with "/": ${path}`);
  }
  return `${API_BASE_URL}${path}`;
}

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export type ApiRequestOptions = {
  method?: HttpMethod;
  /** Serialized as JSON. */
  body?: unknown;
  /** Sent as `Authorization: Bearer <token>` when given. */
  token?: string | null;
  signal?: AbortSignal;
};

export function buildRequestInit({ method, body, token, signal }: ApiRequestOptions): RequestInit {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  return {
    method: method ?? (body === undefined ? "GET" : "POST"),
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
    cache: "no-store",
  };
}

async function readJson(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/**
 * Call the backend and return the parsed JSON body.
 *
 * Throws ApiError for HTTP errors, unreachable servers and unreadable success bodies.
 * An aborted request rethrows the AbortError untouched so callers can ignore it.
 */
export async function apiFetch<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const url = apiUrl(path);
  let res: Response;
  let body: unknown;
  try {
    res = await fetch(url, buildRequestInit(options));
    body = await readJson(res);
  } catch (err) {
    if (options.signal?.aborted) throw err;
    throw new ApiError(0, NETWORK_MESSAGE);
  }

  if (!res.ok) throw toApiError(res.status, body);
  if (body === undefined) throw new ApiError(res.status, UNEXPECTED_RESPONSE_MESSAGE);
  return body as T;
}

export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/api/health", { signal });
}
