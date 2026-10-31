import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { can, useMe } from "@/auth/auth";

export const visitorLogKey = ["features", "unknowns"] as const;
export const visitorCountsKey = ["unknowns", "counts"] as const;

/**
 * The unknown visitor log (Settings > System > Unknown visitors): faces the cameras saw that matched nobody, kept for
 * review. Only admins and guards may see it (unknowns:read); the queries never run for other roles, whose requests
 * the API would refuse. `enabled`: the log is on (the cameras record new visitors); `waiting`: visits not reviewed
 * yet, which keep the menu item there after the log is switched off.
 */
export function useVisitorLog(): { allowed: boolean; enabled: boolean; known: boolean; waiting: number } {
  const { data: me } = useMe();
  const allowed = can(me, "unknowns:read");
  const mode = useQuery({
    queryKey: visitorLogKey,
    queryFn: () => api.get("/api/settings/unknowns"),
    enabled: allowed,
    staleTime: 60_000,
  });
  const counts = useQuery({
    queryKey: visitorCountsKey,
    queryFn: () => api.get("/api/unknowns/counts"),
    enabled: allowed,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
  return {
    allowed,
    enabled: mode.data?.enabled === true,
    known: mode.isSuccess,
    waiting: counts.data?.new ?? 0,
  };
}
