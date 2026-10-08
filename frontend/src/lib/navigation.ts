/**
 * Primary navigation. Routes only: which items a role sees is decided later (Layer 11/12)
 * and is a UX choice, never authorization; the backend enforces access.
 */

export type NavItemId = "dashboard" | "chat";

export type NavItem = {
  id: NavItemId;
  href: string;
  label: string;
};

export const NAV_ITEMS: readonly NavItem[] = [
  { id: "dashboard", href: "/dashboard", label: "Dashboard" },
  { id: "chat", href: "/chat", label: "Assistant" },
];

/** Whether a nav item is the current page, including its sub-pages. */
export function isActivePath(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}
