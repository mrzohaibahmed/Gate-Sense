import { useQuery } from "@tanstack/react-query";
import { Bell, CameraOff, LogOut, Menu, Moon, Search, Sun, SunMoon, UserRoundX } from "lucide-react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

import { api } from "@/api/client";
import { can, useMe } from "@/auth/auth";
import { Badge } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { NativeSelect } from "@/components/ui/input";
import { Tooltip } from "@/components/ui/misc";
import { useLive } from "@/live/LiveProvider";
import { cn, today } from "@/lib/utils";

import { useFilters } from "./filters";
import { setTheme } from "./theme";

export function TopBar({ onMenu, onSearch }: { onMenu: () => void; onSearch: () => void }) {
  return (
    <header className="sticky top-0 z-20 flex h-12 items-center gap-2 border-b bg-card px-2 md:px-4">
      <Button variant="ghost" size="icon" className="md:hidden" onClick={onMenu} aria-label="Open the menu">
        <Menu />
      </Button>
      <SiteSwitcher />
      <DatePicker />
      <button
        type="button"
        onClick={onSearch}
        className="ml-auto flex h-8 min-w-0 items-center gap-2 rounded-md border border-input bg-background px-2 text-sm text-muted-foreground hover:bg-muted md:w-64"
        aria-label="Search (Ctrl+K)"
      >
        <Search className="size-4 shrink-0" />
        <span className="hidden truncate md:inline">Search people, pages…</span>
        <kbd className="ml-auto hidden rounded border bg-muted px-1 text-xs md:inline">Ctrl K</kbd>
      </button>
      <LiveDot />
      <Notifications />
      <UserMenu />
    </header>
  );
}

function SiteSwitcher() {
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

function DatePicker() {
  const { date, setDate } = useFilters();
  return (
    <input
      type="date"
      aria-label="Day"
      value={date}
      max={today()}
      onChange={(e) => e.target.value && setDate(e.target.value)}
      className="h-8 rounded-md border border-input bg-card px-2 text-sm"
    />
  );
}

const DOT = {
  open: { colour: "bg-ok", label: "Live: connected" },
  connecting: { colour: "bg-warn animate-pulse", label: "Live: connecting…" },
  closed: { colour: "bg-destructive", label: "Live: disconnected, retrying" },
} as const;

function LiveDot() {
  const { status } = useLive();
  const { colour, label } = DOT[status];
  return (
    <Tooltip content={label}>
      <span role="status" aria-label={label} className="flex size-8 items-center justify-center">
        <span className={cn("size-2.5 rounded-full", colour)} />
      </span>
    </Tooltip>
  );
}

type Notice = { key: string; icon: typeof Bell; text: string; to: string; tone: "missing" | "error" | "warn" };

/** Today's shifts that ended without an OUT, and cameras with an error or warning. */
function Notifications() {
  const { data: me } = useMe();
  const { cameras } = useLive();
  const { siteId } = useFilters();
  const missing = useQuery({
    queryKey: ["notifications", "missing", siteId],
    queryFn: () =>
      api.get("/api/attendance", {
        query: { from: today(), to: today(), status: "missing OUT", page_size: 20, site_id: siteId },
      }),
    enabled: can(me, "attendance:read"),
    refetchInterval: 60_000,
  });
  const notices: Notice[] = [];
  for (const row of missing.data?.items ?? []) {
    notices.push({
      key: `m${row.person_id}`,
      icon: UserRoundX,
      text: `${row.name}: no OUT for the ${row.shift ?? ""} shift`,
      to: `/attendance?status=missing%20OUT`,
      tone: "missing",
    });
  }
  for (const cam of Object.values(cameras)) {
    const problem = cam.error ?? cam.warning ?? (cam.status === "not responding" ? "not responding" : null);
    if (problem) {
      notices.push({
        key: `c${cam.name}`,
        icon: CameraOff,
        text: `${cam.name}: ${problem}`,
        to: "/live",
        tone: "error",
      });
    }
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={`Notifications (${notices.length})`} className="relative">
          <Bell />
          {notices.length > 0 && (
            <span className="absolute top-1 right-1 flex min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] leading-4 text-destructive-foreground">
              {notices.length}
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuLabel>Needs attention</DropdownMenuLabel>
        {notices.length === 0 ? (
          <p className="px-2 py-4 text-center text-sm text-muted-foreground">Nothing right now.</p>
        ) : (
          notices.map((n) => (
            <DropdownMenuItem key={n.key} asChild>
              <Link to={n.to}>
                <n.icon className={cn(n.tone === "missing" ? "text-status-missing" : "text-destructive")} />
                <span className="truncate">{n.text}</span>
              </Link>
            </DropdownMenuItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function UserMenu() {
  const { data: me } = useMe();
  const navigate = useNavigate();
  const logout = async () => {
    try {
      await api.post("/api/auth/logout");
    } catch {
      /* already logged out */
    }
    window.location.assign("/login"); // a full reload drops every cached query
  };
  if (!me) return null;
  const initials = (me.full_name || me.username)
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" className="gap-2 px-1 md:px-2" aria-label="Account">
          <span className="flex size-6 items-center justify-center rounded-full bg-accent text-xs font-semibold text-accent-foreground">
            {initials}
          </span>
          <span className="hidden max-w-32 truncate md:inline">{me.full_name || me.username}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <div className="flex flex-col gap-1 px-2 py-1.5">
          <span className="truncate font-medium">{me.full_name || me.username}</span>
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            {me.username} <Badge tone="accent">{me.role}</Badge>
          </span>
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setTheme("light")}>
          <Sun /> Light
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setTheme("dark")}>
          <Moon /> Dark
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setTheme("system")}>
          <SunMoon /> Like the system
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => navigate("/change-password?voluntary=1")}>Change password</DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            toast.message("Logging out…");
            void logout();
          }}
        >
          <LogOut /> Log out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
