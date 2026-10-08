"use client";

import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth/AuthProvider";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge } from "@/components/ui/Badge";
import { ButtonLink } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { LoadingState } from "@/components/ui/Spinner";
import {
  INITIAL_DASHBOARD_STATE,
  ROLE_DESCRIPTIONS,
  dashboardKpis,
  dashboardPanels,
  firstName,
  greeting,
  loadDashboard,
  type DashboardState,
  type PanelId,
} from "@/lib/dashboard";
import { ROLE_LABELS } from "@/lib/roles";
import type { DashboardResponse } from "@/lib/types";
import { KpiGrid } from "./KpiGrid";
import { BusinessOverviewPanel, InventoryOperationsPanel, ProcurementOverviewPanel } from "./Panels";
import styles from "./Dashboard.module.css";

export function Dashboard() {
  const { user, authFetch } = useAuth();
  const [state, setState] = useState<DashboardState>(INITIAL_DASHBOARD_STATE);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    loadDashboard(authFetch, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setState(next);
      })
      .catch(() => {
        // Only an aborted load rejects; there is nothing to show for it.
      });
    return () => controller.abort();
  }, [authFetch, attempt]);

  function retry() {
    setState(INITIAL_DASHBOARD_STATE);
    setAttempt((n) => n + 1);
  }

  // The header uses the signed-in user (GET /api/auth/me) so it shows while data loads.
  const role = state.status === "ready" ? state.data.user.role : user?.role;
  const name = state.status === "ready" ? state.data.user.full_name : (user?.full_name ?? "");

  return (
    <>
      <PageHeader
        title={`${greeting(new Date())}${name ? `, ${firstName(name)}` : ""}`}
        description={
          role && (
            <span className={styles.headerMeta}>
              <Badge tone="accent">{ROLE_LABELS[role]}</Badge>
              {ROLE_DESCRIPTIONS[role]}
            </span>
          )
        }
        actions={
          <ButtonLink href="/chat" variant="primary">
            Ask the assistant
          </ButtonLink>
        }
      />
      {state.status === "loading" && (
        <Card>
          <LoadingState label="Loading your dashboard…" />
        </Card>
      )}
      {state.status === "error" && (
        <Card>
          <ErrorState title="Unable to load the dashboard" message={state.message} onRetry={retry} />
        </Card>
      )}
      {state.status === "ready" && <DashboardContent data={state.data} />}
    </>
  );
}

function DashboardContent({ data }: { data: DashboardResponse }) {
  const kpis = dashboardKpis(data);
  const panels = dashboardPanels(data);

  if (kpis.length === 0 && panels.length === 0) {
    return (
      <Card>
        <EmptyState
          title="Nothing to show yet"
          description="There is no overview data for your role. You can still ask the assistant about the records you have access to."
        />
      </Card>
    );
  }

  return (
    <div className={styles.content}>
      {kpis.length > 0 && <KpiGrid kpis={kpis} />}
      {panels.map((panel) => (
        <Panel key={panel} panel={panel} data={data} />
      ))}
    </div>
  );
}

function Panel({ panel, data }: { panel: PanelId; data: DashboardResponse }) {
  switch (panel) {
    case "inventory_operations":
      return data.inventory && <InventoryOperationsPanel inventory={data.inventory} />;
    case "procurement_overview":
      return data.procurement && <ProcurementOverviewPanel procurement={data.procurement} />;
    case "business_overview":
      return data.analytics && <BusinessOverviewPanel analytics={data.analytics} />;
  }
}
