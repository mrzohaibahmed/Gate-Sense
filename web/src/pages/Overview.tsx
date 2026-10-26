import { useQuery } from "@tanstack/react-query";
import { Activity, CameraOff, Inbox, TriangleAlert, UserCheck, UserRoundX } from "lucide-react";
import { Link } from "react-router";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts";

import { api, resourceUrl } from "@/api/client";
import type { AttendanceEvent, CameraStatus, Overview, Status } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { useFilters } from "@/app/filters";
import { CameraStateDot, DirectionBadge, StatusBadge } from "@/components/badges";
import { PageSkeleton } from "@/components/states";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, Td, Th } from "@/components/ui/misc";
import { useLive } from "@/live/LiveProvider";
import { timeOn } from "@/lib/days";
import { cn, hhmm, today } from "@/lib/utils";

const NEEDS_ATTENTION: Status[] = ["missing IN", "missing OUT", "check"];

export function OverviewPage() {
  const { date, siteId } = useFilters();
  const isToday = date === today();
  const overview = useQuery({
    queryKey: ["overview", date, siteId],
    queryFn: () => api.get("/api/overview", { query: { date, site_id: siteId } }),
    refetchInterval: 60_000,
  });
  if (overview.isPending) return <PageSkeleton />;
  if (overview.error) return <ErrorState error={overview.error} retry={() => void overview.refetch()} />;
  const o = overview.data;
  return (
    <>
      <PageHeader title={isToday ? "Today" : `Overview · ${date}`} />
      <div className="flex flex-col gap-4 px-4 pb-4">
        <HealthBanner />
        <Kpis o={o} isToday={isToday} />
        <div className="grid gap-4 lg:grid-cols-3">
          <ArrivalsChart o={o} />
          <CameraHealth cameras={o.cameras} />
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <LiveFeed date={date} siteId={siteId} />
          <NeedsAttention date={date} siteId={siteId} isToday={isToday} />
        </div>
      </div>
    </>
  );
}

/** What GET /api/health says someone should look at (a camera down, little disk space, a failed or overdue
 * backup); nothing when all is well. */
export function HealthBanner() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api.get("/api/health"),
    refetchInterval: 30_000,
  });
  const problems = health.data?.problems ?? [];
  if (problems.length === 0) return null;
  return (
    <div
      role="alert"
      className="flex gap-3 rounded-lg border border-status-missing/40 bg-status-missing/10 px-4 py-3 text-sm"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-status-missing" aria-hidden />
      <div className="flex flex-col gap-1">
        <span className="font-medium">{problems.length === 1 ? "Something needs a look" : "These need a look"}</span>
        <ul className="list-disc pl-4 text-muted-foreground">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function Tile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
  tone: "inside" | "left" | "missing" | "absent" | "accent";
}) {
  const bar = {
    inside: "bg-status-inside",
    left: "bg-status-left",
    missing: "bg-status-missing",
    absent: "bg-status-absent",
    accent: "bg-primary",
  }[tone];
  return (
    <Card className="relative overflow-hidden">
      <span className={cn("absolute inset-y-0 left-0 w-1", bar)} aria-hidden />
      <div className="flex flex-col gap-1 py-2 pr-2 pl-4">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className="text-2xl leading-8 font-semibold">{value}</span>
        {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
      </div>
    </Card>
  );
}

function Kpis({ o, isToday }: { o: Overview; isToday: boolean }) {
  return (
    <section aria-label="Key figures" className="grid grid-cols-2 gap-4 sm:grid-cols-4 xl:grid-cols-7">
      <Tile
        label="Present / scheduled"
        value={`${o.present} / ${o.scheduled}`}
        sub="came in for their shift"
        tone="accent"
      />
      <Tile
        label="Inside now"
        value={o.inside_now ?? "—"}
        sub={isToday ? "shift running, last pass IN" : "only for today"}
        tone="inside"
      />
      <Tile label="On time" value={o.on_time} tone="left" />
      <Tile label="Late" value={o.late} sub="IN after start + grace" tone="missing" />
      <Tile label="Early leave" value={o.early_leave} sub="OUT before end − grace" tone="missing" />
      <Tile label="Overtime" value={o.overtime} sub="OUT after the end" tone="accent" />
      <Tile label="Not in yet" value={o.not_in_yet} sub="IN window still open" tone="left" />
      <Tile label="Missing IN" value={o.missing_in} sub="OUT without an IN" tone="missing" />
      <Tile label="Missing OUT" value={o.missing_out} sub="no OUT after the IN" tone="missing" />
      <Tile label="Check" value={o.check} sub="passes outside their windows" tone="missing" />
      <Tile label="Absent" value={o.absent} tone="absent" />
      <Tile label="Off" value={o.off_day} sub="OFF on the roster" tone="left" />
      <Tile label="Unscheduled" value={o.unscheduled} sub="passes, no roster entry" tone="left" />
    </section>
  );
}

function ArrivalsChart({ o }: { o: Overview }) {
  const first = o.arrivals_by_hour.findIndex((h) => h.arrivals > 0);
  const last = o.arrivals_by_hour.findLastIndex((h) => h.arrivals > 0);
  const data = o.arrivals_by_hour.slice(Math.max(0, Math.min(first, 6) - 1), Math.max(last, 18) + 2);
  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardTitle>Arrivals per hour</CardTitle>
        <span className="text-xs text-muted-foreground">first IN of each person</span>
      </CardHeader>
      <CardContent className="h-56 p-2">
        {first === -1 ? (
          <EmptyState icon={<Activity />} title="No arrivals yet" className="py-8" />
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--border)" />
              <XAxis
                dataKey="hour"
                tickFormatter={(h: number) => `${String(h).padStart(2, "0")}`}
                tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                allowDecimals={false}
                tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
                tickLine={false}
                axisLine={false}
              />
              <ChartTooltip
                cursor={{ fill: "var(--muted)" }}
                contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", fontSize: 12 }}
                labelFormatter={(h) =>
                  `${String(h).padStart(2, "0")}:00 – ${String(Number(h) + 1).padStart(2, "0")}:00`
                }
                formatter={(v) => [v, "arrivals"]}
              />
              <Bar dataKey="arrivals" fill="var(--primary)" radius={[3, 3, 0, 0]} maxBarSize={28} />
            </BarChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}

function CameraHealth({ cameras }: { cameras: CameraStatus[] }) {
  const { cameras: live } = useLive();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Cameras</CardTitle>
        <Link to="/live" className="text-xs text-primary hover:underline">
          Live view
        </Link>
      </CardHeader>
      {cameras.length === 0 ? (
        <EmptyState icon={<CameraOff />} title="No cameras" />
      ) : (
        <ul className="divide-y">
          {cameras.map((c) => {
            const l = live[c.camera];
            const state = l?.status ?? c.state;
            const problem = l ? (l.error ?? l.warning) : (c.last_error ?? c.warning);
            return (
              <li key={c.id} className="flex items-center gap-2 px-4 py-2">
                <CameraStateDot state={state} />
                <span className="font-medium">{c.camera}</span>
                <DirectionBadge direction={c.direction} />
                <span className="ml-auto text-xs text-muted-foreground">
                  {state === "running" ? `${(l?.fps ?? c.fps ?? 0).toFixed(1)} fps · ` : `${state} · `}
                  {c.events} today
                </span>
                {problem && (
                  <Badge tone="error" className="max-w-40 truncate" title={problem}>
                    {problem}
                  </Badge>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

export function EventRow({ e, fresh }: { e: AttendanceEvent; fresh?: boolean }) {
  return (
    <li className={cn("flex items-center gap-2 px-4 py-2", fresh && "bg-accent/40")}>
      {e.snapshot_url ? (
        <img src={resourceUrl(e.snapshot_url)} alt="" className="size-8 rounded object-cover" loading="lazy" />
      ) : (
        <span className="size-8 rounded bg-muted" aria-hidden />
      )}
      <div className="flex min-w-0 flex-col">
        <span className="truncate font-medium">{e.name}</span>
        <span className="text-xs text-muted-foreground">
          {e.employee_code} · {e.camera}
          {e.manual && " · manual"}
          {e.voided && " · voided"}
        </span>
      </div>
      <DirectionBadge direction={e.direction} className="ml-auto" />
      <span className="w-12 text-right text-xs text-muted-foreground">{hhmm(e.timestamp)}</span>
    </li>
  );
}

/** Events of the day: the newest from the API, plus those arriving live (marked). */
function LiveFeed({ date, siteId }: { date: string; siteId: number | null }) {
  const { events: live } = useLive();
  const recent = useQuery({
    queryKey: ["events", "feed", date, siteId],
    queryFn: () => api.get("/api/events", { query: { from: date, to: date, site_id: siteId, page_size: 20 } }),
  });
  const liveIds = new Set(live.map((e) => e.id));
  const fromLive = live.filter((e) => e.timestamp.startsWith(date) && (siteId === null || e.site_id === siteId));
  const merged = [...fromLive, ...(recent.data?.items ?? []).filter((e) => !liveIds.has(e.id))]
    .sort((a, b) => b.timestamp.localeCompare(a.timestamp))
    .slice(0, 20);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Live events</CardTitle>
        <span className="text-xs text-muted-foreground">newest first</span>
      </CardHeader>
      {recent.error ? (
        <ErrorState error={recent.error} retry={() => void recent.refetch()} />
      ) : recent.isPending ? (
        <div className="h-40" />
      ) : merged.length === 0 ? (
        <EmptyState icon={<Inbox />} title="No events yet">
          They appear here as people pass the cameras.
        </EmptyState>
      ) : (
        <ul className="max-h-96 divide-y overflow-y-auto" aria-label="Events" aria-live="polite">
          {merged.map((e) => (
            <EventRow key={e.id} e={e} fresh={liveIds.has(e.id)} />
          ))}
        </ul>
      )}
    </Card>
  );
}

function NeedsAttention({ date, siteId, isToday }: { date: string; siteId: number | null; isToday: boolean }) {
  const rows = useQuery({
    queryKey: ["attendance", "attention", date, siteId],
    queryFn: () =>
      api.get("/api/attendance", { query: { from: date, to: date, site_id: siteId, page_size: 500, sort: "name" } }),
  });
  // Missing OUT is only given once the shift's OUT window has closed, so it is never just "still at work".
  const items = (rows.data?.items ?? []).filter((r) => NEEDS_ATTENTION.includes(r.status));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Needs attention</CardTitle>
        <Link to="/attendance" className="text-xs text-primary hover:underline">
          Attendance
        </Link>
      </CardHeader>
      {rows.error ? (
        <ErrorState error={rows.error} retry={() => void rows.refetch()} />
      ) : rows.isPending ? (
        <div className="h-40" />
      ) : items.length === 0 ? (
        <EmptyState icon={<UserCheck />} title="Nothing to fix">
          {isToday
            ? "Missing INs, days to check, and missing OUTs once a shift's OUT window has closed show here."
            : "Every shift's IN has its OUT."}
        </EmptyState>
      ) : (
        <div className="max-h-96 overflow-auto">
          <table className="w-full text-sm">
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Status</Th>
                <Th>First IN</Th>
                <Th>Last OUT</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr key={r.person_id}>
                  <Td>
                    <Link
                      to={`/attendance?from=${date}&to=${date}&person=${r.person_id}`}
                      className="inline-flex items-center gap-1 hover:underline"
                    >
                      <UserRoundX className="size-3.5 text-status-missing" aria-hidden />
                      {r.name}
                    </Link>
                  </Td>
                  <Td>
                    <StatusBadge status={r.status} />
                  </Td>
                  <Td>{timeOn(r.first_in, r.date)}</Td>
                  <Td>{timeOn(r.last_out, r.date)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
