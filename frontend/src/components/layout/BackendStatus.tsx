"use client";

import { useEffect, useState } from "react";

import { getHealth } from "@/lib/api";
import styles from "./BackendStatus.module.css";

type Status = "checking" | "online" | "offline";

const LABELS: Record<Status, string> = {
  checking: "Checking service…",
  online: "Service online",
  offline: "Service unavailable",
};

/** Small backend reachability indicator (GET /api/health). Shows no URLs or environment details. */
export function BackendStatus() {
  const [status, setStatus] = useState<Status>("checking");

  useEffect(() => {
    const controller = new AbortController();
    getHealth(controller.signal)
      .then((health) => setStatus(health.status === "ok" ? "online" : "offline"))
      .catch(() => {
        if (!controller.signal.aborted) setStatus("offline");
      });
    return () => controller.abort();
  }, []);

  return (
    <span className={styles.status} data-status={status} role="status">
      <span className={styles.dot} aria-hidden="true" />
      {LABELS[status]}
    </span>
  );
}
