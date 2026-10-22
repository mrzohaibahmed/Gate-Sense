import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Join class names, letting later Tailwind classes override earlier ones (shadcn/ui's helper). */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** Today as YYYY-MM-DD in local time (the API's dates are local calendar days). */
export function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** "08:05" from an ISO local timestamp ("2026-10-22T08:05:12"); "" for null. */
export function hhmm(iso: string | null | undefined): string {
  return iso ? iso.slice(11, 16) : "";
}

/** "8 h 05 min" from minutes; "" for null. */
export function duration(minutes: number | null | undefined): string {
  if (minutes == null) return "";
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} min`;
}
