import { today } from "@/lib/utils";

export type Preset = "today" | "week" | "month" | "last-month" | "custom";

function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** The first and last day of a preset, as YYYY-MM-DD. Weeks start on Monday. */
export function presetRange(preset: Exclude<Preset, "custom">, todayText: string = today()): [string, string] {
  const [y, m, d] = todayText.split("-").map(Number) as [number, number, number];
  const now = new Date(y, m - 1, d);
  switch (preset) {
    case "today":
      return [todayText, todayText];
    case "week": {
      const monday = new Date(y, m - 1, d - ((now.getDay() + 6) % 7));
      return [ymd(monday), todayText];
    }
    case "month":
      return [ymd(new Date(y, m - 1, 1)), todayText];
    case "last-month":
      return [ymd(new Date(y, m - 2, 1)), ymd(new Date(y, m - 1, 0))];
  }
}
