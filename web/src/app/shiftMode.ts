import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { useMe } from "@/auth/auth";

export const shiftModeKey = ["features", "shifts"] as const;

/**
 * Are shifts on (Settings > System > Shifts)? On: attendance by roster day, the Roster page and Settings > Shifts.
 * Off: attendance by calendar day (first IN, last OUT), and both are hidden. The API reads it on every request; this
 * asks once a minute and whenever a change arrives over the WebSocket. Shifts are off by default, and until the
 * answer arrives they count as off (so the Roster menu item never flashes on an install without shifts).
 */
/** Has the answer arrived yet? (A page can wait instead of showing the "off" state for a moment.) */
export function useShiftModeKnown(): boolean {
  const { data: me } = useMe();
  return useQuery({ queryKey: shiftModeKey, queryFn: () => api.get("/api/settings/shifts"), enabled: !!me }).isSuccess;
}

export function useShiftsEnabled(): boolean {
  const { data: me } = useMe();
  const mode = useQuery({
    queryKey: shiftModeKey,
    queryFn: () => api.get("/api/settings/shifts"),
    enabled: !!me,
    staleTime: 60_000,
  });
  return mode.data?.enabled === true;
}
