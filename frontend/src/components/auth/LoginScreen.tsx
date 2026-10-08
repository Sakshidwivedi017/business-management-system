"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { LogoMark } from "@/components/ui/icons";
import { useAuth } from "./AuthProvider";
import { LoginForm } from "./LoginForm";
import { FullPageLoading } from "./RequireAuth";
import styles from "./LoginScreen.module.css";

export function LoginScreen() {
  const { status } = useAuth();
  const router = useRouter();

  // Signed in (just now, or already in this tab): continue to the application.
  useEffect(() => {
    if (status === "authenticated") router.replace("/dashboard");
  }, [status, router]);

  if (status === "loading" || status === "authenticated") {
    return <FullPageLoading label={status === "authenticated" ? "Signing you in…" : "Loading…"} />;
  }

  return (
    <main className={styles.page}>
      <div className={styles.panel}>
        <div className={styles.brand}>
          <LogoMark width={32} height={32} />
          <span className={styles.brandName}>Lecxe Operations</span>
        </div>
        <h1 className={styles.title}>Sign in</h1>
        <p className={styles.text}>Inventory and procurement operations, with an assistant for your role.</p>
        <LoginForm />
        <p className={styles.footnote}>Access is managed by your administrator.</p>
      </div>
    </main>
  );
}
