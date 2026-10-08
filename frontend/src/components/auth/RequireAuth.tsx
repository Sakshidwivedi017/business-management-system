"use client";

import { useRouter } from "next/navigation";
import { Fragment, useEffect, type ReactNode } from "react";

import { Button } from "@/components/ui/Button";
import { ErrorState } from "@/components/ui/ErrorState";
import { LoadingState } from "@/components/ui/Spinner";
import { useAuth } from "./AuthProvider";
import styles from "./FullPage.module.css";

/**
 * Renders its children only for a signed-in user; otherwise a full-page state, and a
 * redirect to /login once the session is known to be missing. This is navigation UX:
 * the backend still checks the token on every request.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status, user, error, retry, logout } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === "unauthenticated") router.replace("/login");
  }, [status, router]);

  // Keyed by user: page state from one user's session can never carry over into another's.
  if (status === "authenticated" && user) return <Fragment key={user.id}>{children}</Fragment>;

  if (status === "error") {
    return (
      <div className={styles.page}>
        <div className={styles.panel}>
          <ErrorState title="Unable to load your session" message={error ?? undefined} onRetry={retry} />
          <div className={styles.secondary}>
            <Button variant="ghost" size="sm" onClick={logout}>
              Sign in again
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return <FullPageLoading label={status === "unauthenticated" ? "Redirecting to sign in…" : "Loading your session…"} />;
}

export function FullPageLoading({ label }: { label: string }) {
  return (
    <div className={styles.page}>
      <LoadingState label={label} />
    </div>
  );
}
