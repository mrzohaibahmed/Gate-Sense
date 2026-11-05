import { useQuery } from "@tanstack/react-query";
import { CalendarDays } from "lucide-react";

import { api } from "@/api/client";
import { can, useMe } from "@/auth/auth";
import { NativeSelect } from "@/components/ui/input";
import { today } from "@/lib/utils";

import { useFilters } from "./filters";

/** The site shared by every screen; nothing when there is only one to choose. */
export function SiteSwitcher() {
  const { data: me } = useMe();
  const { siteId, setSiteId } = useFilters();
  const sites = useQuery({
    queryKey: ["sites"],
    queryFn: () => api.get("/api/sites", { query: { page_size: 500 } }),
    enabled: can(me, "sites:read"),
    staleTime: 300_000,
  });
  const items = sites.data?.items ?? [];
  if (items.length <= 1 && siteId === null) return null; // one site: nothing to switch
  return (
    <NativeSelect
      aria-label="Site"
      className="max-w-40"
      value={siteId ?? ""}
      onChange={(e) => setSiteId(e.target.value ? Number(e.target.value) : null)}
    >
      <option value="">All sites</option>
      {items.map((s) => (
        <option key={s.id} value={s.id}>
          {s.name}
        </option>
      ))}
    </NativeSelect>
  );
}

function longDate(day: string): string {
  const parsed = new Date(`${day}T12:00:00`);
  return parsed.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short", year: "numeric" });
}

/** The day the screens show: the written-out date, with the browser's own date input over it (keyboard included). */
export function DatePicker() {
  const { date, setDate } = useFilters();
  return (
    <span className="relative inline-flex h-10 items-center gap-2 rounded-lg px-2 whitespace-nowrap focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-ring hover:bg-muted">
      <CalendarDays className="size-5 shrink-0 text-foreground" aria-hidden />
      <span aria-hidden>{longDate(date)}</span>
      <input
        type="date"
        aria-label="Day"
        value={date}
        max={today()}
        onChange={(e) => e.target.value && setDate(e.target.value)}
        onClick={(e) => {
          try {
            e.currentTarget.showPicker();
          } catch {
            /* an older browser: its own control takes the click */
          }
        }}
        className="absolute inset-0 w-full cursor-pointer opacity-0"
      />
    </span>
  );
}
