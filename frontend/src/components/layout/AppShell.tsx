import type { ReactNode } from "react";

import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import styles from "./AppShell.module.css";

/** Sidebar, top bar and scrolling main content shared by every signed-in page. */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className={styles.shell}>
      <Sidebar />
      <div className={styles.column}>
        <TopBar />
        <main className={styles.main}>
          <div className={styles.content}>{children}</div>
        </main>
      </div>
    </div>
  );
}
