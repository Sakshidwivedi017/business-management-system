import type { Role } from "./types";

export const ROLES: readonly Role[] = ["inventory_manager", "procurement_manager", "owner"];

/** Display names. Presentation only: the backend decides what a role may do. */
export const ROLE_LABELS: Record<Role, string> = {
  inventory_manager: "Inventory Manager",
  procurement_manager: "Procurement Manager",
  owner: "Owner",
};

export function isRole(value: unknown): value is Role {
  return typeof value === "string" && (ROLES as readonly string[]).includes(value);
}
