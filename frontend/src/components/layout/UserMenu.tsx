"use client";

import { useAuth } from "@/components/auth/AuthProvider";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ROLE_LABELS } from "@/lib/roles";
import styles from "./UserMenu.module.css";

function initials(fullName: string): string {
  const parts = fullName.trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase() || "?";
}

/** Signed-in user, their role and sign-out. Rendered only inside RequireAuth. */
export function UserMenu() {
  const { user, logout } = useAuth();
  if (!user) return null;

  return (
    <div className={styles.user}>
      <Badge tone="accent">{ROLE_LABELS[user.role]}</Badge>
      <div className={styles.identity}>
        <span className={styles.name}>{user.full_name}</span>
        <span className={styles.email}>{user.email}</span>
      </div>
      <span className={styles.avatar} aria-hidden="true">
        {initials(user.full_name)}
      </span>
      <Button variant="ghost" size="sm" onClick={logout}>
        Sign out
      </Button>
    </div>
  );
}
