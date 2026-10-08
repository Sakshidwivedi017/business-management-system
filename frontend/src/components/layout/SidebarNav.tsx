"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ComponentType, SVGProps } from "react";

import { ChatIcon, DashboardIcon } from "@/components/ui/icons";
import { NAV_ITEMS, isActivePath, type NavItemId } from "@/lib/navigation";
import styles from "./Sidebar.module.css";

const ICONS: Record<NavItemId, ComponentType<SVGProps<SVGSVGElement>>> = {
  dashboard: DashboardIcon,
  chat: ChatIcon,
};

export function SidebarNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Main">
      <ul className={styles.nav}>
        {NAV_ITEMS.map((item) => {
          const Icon = ICONS[item.id];
          const active = isActivePath(pathname, item.href);
          return (
            <li key={item.id}>
              <Link
                href={item.href}
                className={`${styles.link} ${active ? styles.active : ""}`}
                aria-current={active ? "page" : undefined}
                title={item.label}
              >
                <Icon />
                <span className={styles.linkLabel}>{item.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
