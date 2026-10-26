import type { Status } from "@/api/types";
import { hhmm } from "@/lib/utils";

/** Every roster-day status, in the order the filters list them. */
export const STATUSES: Status[] = [
  "on time",
  "late",
  "early leave",
  "missing IN",
  "missing OUT",
  "absent",
  "not in yet",
  "check",
  "off day",
  "unscheduled",
];

/** "General 08:00–16:30", "Off" for an OFF day, "—" when there is no roster entry (unscheduled). */
export function shiftLabel(r: {
  shift: string | null;
  shift_start: string | null;
  shift_end: string | null;
  status: Status;
}): string {
  if (r.shift) return `${r.shift} ${hhmm(r.shift_start)}–${hhmm(r.shift_end)}`;
  return r.status === "off day" ? "Off" : "—";
}

/** "06:05 (next day)" when the time is on a later date than the roster day (a night shift's OUT); "" for null. */
export function timeOn(iso: string | null | undefined, day: string): string {
  if (!iso) return "";
  return iso.slice(0, 10) > day ? `${hhmm(iso)} (next day)` : hhmm(iso);
}

/** Monday of the week of a YYYY-MM-DD date, and that date moved by `days`. Weeks start on Monday. */
export function mondayOf(day: string): string {
  const [y, m, d] = day.split("-").map(Number) as [number, number, number];
  const date = new Date(y, m - 1, d);
  return addDays(day, -((date.getDay() + 6) % 7));
}

export function addDays(day: string, days: number): string {
  const [y, m, d] = day.split("-").map(Number) as [number, number, number];
  const date = new Date(y, m - 1, d + days);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
