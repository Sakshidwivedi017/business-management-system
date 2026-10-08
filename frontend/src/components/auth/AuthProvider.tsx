"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import {
  createAuthorizedFetch,
  endSession,
  getCurrentUser,
  login as loginRequest,
  restoreSession,
  startSession,
  type AuthState,
  type Fetcher,
  type SessionEndReason,
} from "@/lib/auth";
import type { ApiRequestOptions } from "@/lib/api";
import { browserSessionStore, type StoredSession } from "@/lib/session";
import type { AuthenticatedUser, LoginRequest } from "@/lib/types";

// setTimeout fires at once for delays above 2^31 - 1 ms; the token lifetime is far shorter anyway.
const MAX_TIMER_DELAY = 2 ** 31 - 1;

type AuthContextValue = {
  status: AuthState["status"];
  user: AuthenticatedUser | null;
  token: string | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  /** Why the last session ended on its own, for the sign-in page; null after a sign-out. */
  endReason: SessionEndReason | null;
  /** Set when the stored session could not be checked; `retry` checks again. */
  error: string | null;
  /** Resolves once signed in; rejects with the ApiError of a failed attempt. */
  login: (credentials: LoginRequest) => Promise<void>;
  logout: () => void;
  retry: () => void;
  /**
   * apiFetch with the session's token. Any 401 (expired, revoked or deactivated) ends the
   * session here, so callers only handle the rejected promise.
   */
  authFetch: Fetcher;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  // The current session for authFetch, which must not change identity on every render.
  const sessionRef = useRef<StoredSession | null>(null);

  useEffect(() => {
    sessionRef.current = state.status === "authenticated" ? state.session : null;
  }, [state]);

  // Restore the tab's session on start-up and on retry.
  useEffect(() => {
    const controller = new AbortController();
    restoreSession(browserSessionStore(), Date.now(), (token) => getCurrentUser(token, controller.signal))
      .then((next) => {
        if (!controller.signal.aborted) setState(next);
      })
      .catch(() => {
        if (!controller.signal.aborted) setState({ status: "error", message: "Unable to load your session." });
      });
    return () => controller.abort();
  }, [attempt]);

  const end = useCallback((reason: SessionEndReason | null, token: string | null = null) => {
    // A late 401 from an earlier session's request must not end a newer session.
    if (token !== null && sessionRef.current?.token !== token) return;
    sessionRef.current = null;
    setState(endSession(browserSessionStore(), reason));
  }, []);

  // End the session when the token expires. The backend still rejects expired tokens on its own.
  const activeSession = state.status === "authenticated" ? state.session : null;
  useEffect(() => {
    if (!activeSession) return;
    const delay = Math.min(Math.max(activeSession.expiresAt - Date.now(), 0), MAX_TIMER_DELAY);
    const timer = setTimeout(() => end("expired", activeSession.token), delay);
    return () => clearTimeout(timer);
  }, [activeSession, end]);

  const login = useCallback(async (credentials: LoginRequest) => {
    const res = await loginRequest(credentials);
    const next = startSession(browserSessionStore(), res, Date.now());
    if (next.status === "authenticated") sessionRef.current = next.session;
    setState(next);
  }, []);

  const logout = useCallback(() => end(null), [end]);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    setAttempt((n) => n + 1);
  }, []);

  // The session is read when a request is made, never during render.
  const authFetch: Fetcher = useCallback(
    <T,>(path: string, options?: ApiRequestOptions) =>
      createAuthorizedFetch(() => sessionRef.current, end)<T>(path, options),
    [end],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      status: state.status,
      user: state.status === "authenticated" ? state.user : null,
      token: state.status === "authenticated" ? state.session.token : null,
      isLoading: state.status === "loading",
      isAuthenticated: state.status === "authenticated",
      endReason: state.status === "unauthenticated" ? state.reason : null,
      error: state.status === "error" ? state.message : null,
      login,
      logout,
      retry,
      authFetch,
    }),
    [state, login, logout, retry, authFetch],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
