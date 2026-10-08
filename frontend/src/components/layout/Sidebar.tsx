import Link from "next/link";

import { LogoMark } from "@/components/ui/icons";
import { SidebarNav } from "./SidebarNav";
import styles from "./Sidebar.module.css";

export function Sidebar() {
  return (
    <aside className={styles.sidebar}>
      <Link href="/dashboard" className={styles.brand}>
        <LogoMark />
        <span className={styles.brandText}>
          <span className={styles.brandName}>Lecxe</span>
          <span className={styles.brandSub}>Operations</span>
        </span>
      </Link>
      <SidebarNav />
    </aside>
  );
}
