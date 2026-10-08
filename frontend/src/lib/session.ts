/**
 * The browser session: the access token and when it expires, kept in sessionStorage.
 *
 * sessionStorage is scoped to one tab, so a new tab starts signed out. Only the token and
 * its expiry are stored; the user is always re-read from GET /api/auth/me. Nothing here is
 * authorization: the backend validates the token on every request.
 */

/** Every key this app writes to sessionStorage starts with this, so sign-out can clear them all. */
export const STORAGE_PREFIX = "lecxe.";
export const SESSION_KEY = `${STORAGE_PREFIX}session`;

export type StoredSession = {
  token: string;
  /** Epoch milliseconds after which the backend will reject the token. */
  expiresAt: number;
};

/** The parts of the Web Storage API used here; a fake can stand in for tests. */
export type SessionStore = Pick<Storage, "getItem" | "setItem" | "removeItem" | "key" | "length">;

/** window.sessionStorage, or null where it is unavailable (server rendering, blocked storage). */
export function browserSessionStore(): SessionStore | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function createSession(token: string, expiresInSeconds: number, now: number): StoredSession {
  return { token, expiresAt: now + expiresInSeconds * 1000 };
}

export function isExpired(session: StoredSession, now: number): boolean {
  return now >= session.expiresAt;
}

/** A stored session in the expected shape, expired or not; null when missing or malformed. */
export function parseSession(raw: string | null): StoredSession | null {
  if (!raw) return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null) return null;
  const { token, expiresAt } = value as Record<string, unknown>;
  if (typeof token !== "string" || !token || typeof expiresAt !== "number" || !Number.isFinite(expiresAt)) {
    return null;
  }
  return { token, expiresAt };
}

export type SessionRead =
  | { kind: "none" }
  | { kind: "expired" }
  | { kind: "valid"; session: StoredSession };

/** Read the stored session. Expired or malformed sessions are removed. */
export function readSession(store: SessionStore | null, now: number): SessionRead {
  if (!store) return { kind: "none" };
  const raw = store.getItem(SESSION_KEY);
  const session = parseSession(raw);
  if (!session) {
    if (raw !== null) store.removeItem(SESSION_KEY);
    return { kind: "none" };
  }
  if (isExpired(session, now)) {
    clearSession(store);
    return { kind: "expired" };
  }
  return { kind: "valid", session };
}

export function writeSession(store: SessionStore | null, session: StoredSession): void {
  store?.setItem(SESSION_KEY, JSON.stringify(session));
}

/** Remove the session and any other state this app keeps for the tab. */
export function clearSession(store: SessionStore | null): void {
  if (!store) return;
  const keys: string[] = [];
  for (let i = 0; i < store.length; i++) {
    const key = store.key(i);
    if (key?.startsWith(STORAGE_PREFIX)) keys.push(key);
  }
  for (const key of keys) store.removeItem(key);
}
