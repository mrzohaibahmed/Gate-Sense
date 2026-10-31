import { useQuery } from "@tanstack/react-query";

import { ApiError, api } from "@/api/client";
import type { Me } from "@/api/types";

export const meKey = ["me"] as const;

/** The logged-in user, or null when not logged in (a 401 here is an answer, not an error). */
export async function fetchMe(): Promise<Me | null> {
  try {
    return await api.get("/api/auth/me");
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export function useMe() {
  return useQuery({ queryKey: meKey, queryFn: fetchMe, staleTime: 60_000, retry: false });
}

export type Permission =
  | "account:self"
  | "users:manage"
  | "cameras:read"
  | "cameras:control"
  | "cameras:manage"
  | "sites:read"
  | "sites:manage"
  | "settings:manage"
  | "live:view"
  | "attendance:read"
  | "attendance:correct"
  | "people:read"
  | "people:manage"
  | "reports:read"
  | "audit:read"
  | "live:decisions"
  | "roster:read"
  | "roster:manage"
  | "shifts:manage"
  | "unknowns:read"
  | "unknowns:manage";

/** Does the user's role have this permission? (The API decides; this only hides what would be refused.) */
export function can(me: Me | null | undefined, permission: Permission): boolean {
  return !!me && me.permissions.includes(permission);
}

export function canAny(me: Me | null | undefined, permissions: readonly Permission[]): boolean {
  return permissions.some((p) => can(me, p));
}
