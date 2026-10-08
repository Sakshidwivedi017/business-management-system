/**
 * Authentication against the backend (POST /api/auth/login, GET /api/auth/me) and the
 * session state transitions AuthProvider uses. Framework-free so it can be unit tested.
 */

import { ApiError, apiFetch, type ApiRequestOptions } from "./api.ts";
import { isRole } from "./roles.ts";
import {
  clearSession,
  createSession,
  isExpired,
  readSession,
  writeSession,
  type SessionStore,
  type StoredSession,
} from "./session.ts";
import type { AuthenticatedUser, LoginRequest, LoginResponse } from "./types";

/** Why a session ended without the user signing out. */
export type SessionEndReason = "expired" | "revoked";

export const SESSION_END_MESSAGES: Record<SessionEndReason, string> = {
  expired: "Your session has expired. Please sign in again.",
  revoked: "Your session is no longer valid. Please sign in again.",
};

const UNEXPECTED_USER_MESSAGE = "The server returned an unexpected response. Please try again.";

export type AuthState =
  | { status: "loading" }
  | { status: "authenticated"; session: StoredSession; user: AuthenticatedUser }
  | { status: "unauthenticated"; reason: SessionEndReason | null }
  /** The stored token could not be checked (server unreachable or failing); it is kept for a retry. */
  | { status: "error"; message: string };

export type Fetcher = <T>(path: string, options?: ApiRequestOptions) => Promise<T>;

// --- API ---------------------------------------------------------------------------------

/** Validate a user object from the backend, rejecting anything not in the AuthenticatedUser shape. */
export function parseAuthenticatedUser(value: unknown): AuthenticatedUser {
  if (typeof value === "object" && value !== null) {
    const { id, email, full_name, role } = value as Record<string, unknown>;
    if (typeof id === "string" && typeof email === "string" && typeof full_name === "string" && isRole(role)) {
      return { id, email, full_name, role };
    }
  }
  throw new ApiError(200, UNEXPECTED_USER_MESSAGE);
}

export async function login(credentials: LoginRequest, fetcher: Fetcher = apiFetch): Promise<LoginResponse> {
  const body: LoginRequest = { email: credentials.email.trim(), password: credentials.password };
  const res = await fetcher<LoginResponse>("/api/auth/login", { method: "POST", body });
  if (typeof res?.access_token !== "string" || !res.access_token || typeof res.expires_in !== "number") {
    throw new ApiError(200, UNEXPECTED_USER_MESSAGE);
  }
  return { ...res, user: parseAuthenticatedUser(res.user) };
}

export async function getCurrentUser(
  token: string,
  signal?: AbortSignal,
  fetcher: Fetcher = apiFetch,
): Promise<AuthenticatedUser> {
  return parseAuthenticatedUser(await fetcher<unknown>("/api/auth/me", { token, signal }));
}

export type LoginField = keyof LoginRequest;
export type LoginFieldErrors = Partial<Record<LoginField, string>>;

// Mirrors the backend's LoginRequest limits; the backend validates again.
const MAX_EMAIL_LENGTH = 254;
const MAX_PASSWORD_LENGTH = 256;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+$/;

/** Client-side checks before sending a sign-in request. */
export function validateLogin({ email, password }: LoginRequest): LoginFieldErrors {
  const errors: LoginFieldErrors = {};
  const trimmed = email.trim();
  if (!trimmed) errors.email = "Enter your email address.";
  else if (trimmed.length > MAX_EMAIL_LENGTH || !EMAIL_PATTERN.test(trimmed)) errors.email = "Enter a valid email address.";
  if (!password) errors.password = "Enter your password.";
  else if (password.length > MAX_PASSWORD_LENGTH) errors.password = "Password is too long.";
  return errors;
}

/** Field errors from a 422 sign-in response, limited to the form's own fields. */
export function loginFieldErrors(error: unknown): LoginFieldErrors {
  const errors: LoginFieldErrors = {};
  if (!(error instanceof ApiError)) return errors;
  for (const { field, message } of error.fieldErrors) {
    if ((field === "email" || field === "password") && !errors[field]) errors[field] = message;
  }
  return errors;
}

/** The message for a failed sign-in. Never shows raw backend or network details. */
export function loginErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "Sign-in failed. Please try again.";
  if (error.status === 401) return "Invalid email or password.";
  if (error.status === 422) return "Please check the highlighted fields.";
  if (error.isNetworkError || error.status >= 500) {
    return "Unable to connect to the service. Please try again.";
  }
  return error.message;
}

// --- session transitions -----------------------------------------------------------------

/**
 * Restore the tab's session on start-up: a stored, unexpired token is only accepted once
 * GET /api/auth/me confirms it. A 401 clears it; other failures keep it for a retry.
 * An aborted check rethrows the AbortError.
 */
export async function restoreSession(
  store: SessionStore | null,
  now: number,
  fetchUser: (token: string) => Promise<AuthenticatedUser>,
): Promise<AuthState> {
  const read = readSession(store, now);
  if (read.kind === "none") return { status: "unauthenticated", reason: null };
  if (read.kind === "expired") return { status: "unauthenticated", reason: "expired" };
  try {
    const user = await fetchUser(read.session.token);
    return { status: "authenticated", session: read.session, user };
  } catch (err) {
    if (err instanceof ApiError && err.isUnauthorized) {
      clearSession(store);
      return { status: "unauthenticated", reason: "revoked" };
    }
    if (err instanceof ApiError) return { status: "error", message: "Unable to load your session." };
    throw err;
  }
}

/** Store a new session from a successful login. */
export function startSession(store: SessionStore | null, res: LoginResponse, now: number): AuthState {
  const session = createSession(res.access_token, res.expires_in, now);
  clearSession(store); // nothing from an earlier session in this tab survives
  writeSession(store, session);
  return { status: "authenticated", session, user: res.user };
}

/** Sign out (reason null) or end the session because the backend no longer accepts it. */
export function endSession(store: SessionStore | null, reason: SessionEndReason | null): AuthState {
  clearSession(store);
  return { status: "unauthenticated", reason };
}

/**
 * A fetcher that sends the current session's token and reports when the session is over:
 * before the request when it has already expired, or after any 401 from the backend.
 * Callers still receive the ApiError, so they can stop what they were doing.
 */
export function createAuthorizedFetch(
  getSession: () => StoredSession | null,
  onSessionEnded: (reason: SessionEndReason, token: string) => void,
  now: () => number = Date.now,
  fetcher: Fetcher = apiFetch,
): Fetcher {
  return async function authFetch<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
    const session = getSession();
    if (!session) throw new ApiError(401, "Please sign in to continue.");
    if (isExpired(session, now())) {
      onSessionEnded("expired", session.token);
      throw new ApiError(401, SESSION_END_MESSAGES.expired);
    }
    try {
      return await fetcher<T>(path, { ...options, token: session.token });
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) onSessionEnded("revoked", session.token);
      throw err;
    }
  };
}
