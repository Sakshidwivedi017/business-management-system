import { ButtonLink } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import {
  formatAmount,
  formatCount,
  humanize,
  poStatusLabel,
} from "@/lib/dashboard";
import type {
  BusinessAnalytics,
  InventoryOverview,
  LocationStock,
  OpenPurchaseOrders,
  ProcurementOverview,
  PurchaseOrdersByStatus,
  TransactionTypeCount,
  VendorSpend,
} from "@/lib/types";
import { StatTable } from "./StatTable";
import styles from "./Dashboard.module.css";

export function InventoryOperationsPanel({ inventory }: { inventory: InventoryOverview }) {
  const inactive = inventory.total_items - inventory.active_items;
  return (
    <Card
      title="Inventory Operations"
      description="Where stock needs attention, and how to act on it."
      actions={
        <ButtonLink href="/chat" size="sm">
          Open assistant
        </ButtonLink>
      }
    >
      <dl className={styles.facts}>
        <div>
          <dt>Low-stock positions</dt>
          <dd>{formatCount(inventory.low_stock.count)}</dd>
        </div>
        <div>
          <dt>Active locations</dt>
          <dd>{formatCount(inventory.active_locations)}</dd>
        </div>
        <div>
          <dt>Active items</dt>
          <dd>{formatCount(inventory.active_items)}</dd>
        </div>
        <div>
          <dt>Inactive items</dt>
          <dd>{formatCount(inactive)}</dd>
        </div>
      </dl>
      <p className={styles.note}>
        A position is one item at one location. It counts as low when its quantity is at or below the minimum level
        {" "}(or {inventory.low_stock.fallback_threshold} where no minimum is set). Ask the assistant which items are low,
        or to record a stock movement; every change is shown to you for confirmation first.
      </p>
    </Card>
  );
}

export function ProcurementOverviewPanel({ procurement }: { procurement: ProcurementOverview }) {
  return (
    <Card title="Procurement Overview" description="Purchase orders by status. Amounts stay in their own currency.">
      <div className={styles.panelGrid}>
        <section>
          <h3 className={styles.subheading}>Purchase orders by status</h3>
          <StatTable<PurchaseOrdersByStatus>
            caption="Purchase orders by status and currency"
            rows={procurement.purchase_orders_by_status}
            rowKey={(row) => `${row.status}-${row.currency}`}
            empty="No purchase orders yet."
            columns={[
              { header: "Status", cell: (row) => poStatusLabel(row.status) },
              { header: "Orders", cell: (row) => formatCount(row.purchase_orders), numeric: true },
              { header: "Value", cell: (row) => formatAmount(row.total_amount, row.currency), numeric: true },
            ]}
          />
        </section>
        <section>
          <h3 className={styles.subheading}>Open orders</h3>
          <StatTable<OpenPurchaseOrders>
            caption="Open purchase orders by currency"
            rows={procurement.open_purchase_orders}
            rowKey={(row) => row.currency}
            empty="No open purchase orders."
            columns={[
              { header: "Currency", cell: (row) => row.currency },
              { header: "Orders", cell: (row) => formatCount(row.purchase_orders), numeric: true },
              { header: "Lines pending", cell: (row) => formatCount(row.lines_pending_delivery), numeric: true },
              { header: "Value", cell: (row) => formatAmount(row.total_amount, row.currency), numeric: true },
            ]}
          />
        </section>
      </div>
    </Card>
  );
}

export function BusinessOverviewPanel({ analytics }: { analytics: BusinessAnalytics }) {
  const { days, by_type } = analytics.recent_transactions;
  return (
    <Card title="Business Overview" description="Stock across locations, recent movements and top vendors.">
      <div className={styles.panelGrid}>
        <section>
          <h3 className={styles.subheading}>Stock by location</h3>
          <StatTable<LocationStock>
            caption="Active items in and out of stock per location"
            rows={analytics.stock_by_location}
            rowKey={(row) => row.location_name}
            empty="No stocked locations."
            columns={[
              { header: "Location", cell: (row) => row.location_name },
              { header: "In stock", cell: (row) => formatCount(row.items_in_stock), numeric: true },
              { header: "Out of stock", cell: (row) => formatCount(row.items_out_of_stock), numeric: true },
            ]}
          />
        </section>
        <section>
          <h3 className={styles.subheading}>Movements, last {days} days</h3>
          <StatTable<TransactionTypeCount>
            caption={`Inventory transactions by type in the last ${days} days`}
            rows={by_type}
            rowKey={(row) => row.transaction_type}
            empty={`No stock movements in the last ${days} days.`}
            columns={[
              { header: "Type", cell: (row) => humanize(row.transaction_type) },
              { header: "Transactions", cell: (row) => formatCount(row.transactions), numeric: true },
            ]}
          />
        </section>
        <section className={styles.wide}>
          <h3 className={styles.subheading}>Top vendors by spend</h3>
          <StatTable<VendorSpend>
            caption="Vendors with the highest purchase order value"
            rows={analytics.top_vendors_by_spend}
            rowKey={(row) => `${row.vendor_name}-${row.currency}`}
            empty="No purchase orders yet."
            columns={[
              { header: "Vendor", cell: (row) => row.vendor_name },
              { header: "Orders", cell: (row) => formatCount(row.purchase_orders), numeric: true },
              { header: "Spend", cell: (row) => formatAmount(row.total_amount, row.currency), numeric: true },
            ]}
          />
        </section>
      </div>
    </Card>
  );
}
