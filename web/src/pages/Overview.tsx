import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  CameraOff,
  ChevronRight,
  CircleCheck,
  Inbox,
  TriangleAlert,
  Users,
  WifiOff,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api, resourceUrl } from "@/api/client";
import type { AttendanceEvent, AttendanceRow, CameraStatus, Direction, Overview, Status } from "@/api/types";
import { useFilters } from "@/app/filters";
import { DatePicker, SiteSwitcher } from "@/app/PageFilters";
import { can, useMe } from "@/auth/auth";
import { Avatar } from "@/components/Avatar";
import { CameraStateDot, DirectionBadge, StatusBadge } from "@/components/badges";
import { GateIllustration } from "@/components/GateIllustration";
import { Button } from "@/components/ui/button";
import { Badge, Card, Separator, Skeleton } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { EmptyState, ErrorState } from "@/components/ui/misc";
import { useLive } from "@/live/LiveProvider";
import { timeOn } from "@/lib/days";
import { healthNotices } from "@/lib/health";
import { cn, hhmm, today } from "@/lib/utils";

const PREVIEW = 5;

export function OverviewPage() {
  const { date, siteId } = useFilters();
  const overview = useQuery({
    queryKey: ["overview", date, siteId],
    queryFn: () => api.get("/api/overview", { query: { date, site_id: siteId } }),
    refetchInterval: 60_000,
  });
  const o = overview.data;
  return (
    <div className="relative mx-auto flex max-w-[1680px] flex-col gap-6 overflow-hidden px-4 py-6 lg:px-6">
      <header className="grid items-center gap-4 xl:grid-cols-[minmax(0,18fr)_minmax(0,7fr)]">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          <div className="mr-auto">
            <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">People in motion.</h1>
            <p className="mt-1 text-base text-muted-foreground">Real-time attendance at a glance.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <SiteSwitcher />
            <DatePicker />
          </div>
          <SystemNotice />
        </div>
        {o ? <Summary o={o} /> : overview.isPending ? <Skeleton className="h-24 rounded-2xl" /> : null}
      </header>
      <LiveNotice />
      {overview.error && o && (
        <p role="status" className="text-sm text-status-missing">
          These figures couldn't be refreshed: they show the last answer from the server.
        </p>
      )}
      <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,18fr)_minmax(0,7fr)]">
        <div className="flex min-w-0 flex-col gap-6">
          {overview.isPending ? (
            <FlowSkeleton />
          ) : !o ? (
            <Card className="rounded-2xl">
              <ErrorState error={overview.error} retry={() => void overview.refetch()} />
            </Card>
          ) : (
            <>
              <Flow o={o} isToday={date === today()} />
              <ArrivalsChart o={o} />
              {o.shifts && <ShiftStatuses o={o} />}
            </>
          )}
        </div>
        <Card className="flex min-w-0 flex-col gap-4 rounded-2xl p-4">
          <Register date={date} siteId={siteId} />
          <Separator />
          <RecentEvents date={date} siteId={siteId} />
          <Separator />
          <Attention o={o} failed={!!overview.error} date={date} />
        </Card>
      </div>
    </div>
  );
}

/** The live feed is down: the counts only refresh once a minute until it is back. */
function LiveNotice() {
  const { status } = useLive();
  if (status !== "closed") return null;
  return (
    <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
      <WifiOff className="size-4 shrink-0" aria-hidden />
      Live updates are disconnected, retrying. The figures may be up to a minute old.
    </p>
  );
}

/** What GET /api/health says someone should look at, in a few words; nothing when all is well. The full text is one
 * click away for administrators (and stays on Settings > System). */
export function SystemNotice() {
  const { data: me } = useMe();
  const [open, setOpen] = useState(false);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api.get("/api/health"),
    refetchInterval: 30_000,
  });
  const notices = healthNotices(health.data);
  const first = notices[0];
  if (!first) return null;
  const technical = can(me, "settings:manage");
  return (
    <>
      <div
        role="alert"
        className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-status-missing/40 bg-status-missing/10 px-3 py-2 text-sm"
      >
        <TriangleAlert className="size-4 shrink-0 text-status-missing" aria-hidden />
        <span className="min-w-0">
          <span className="font-medium">{first.title}</span>
          <span className="text-muted-foreground"> · {first.detail}</span>
          {notices.length > 1 && <span className="text-muted-foreground"> · {notices.length - 1} more</span>}
        </span>
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="inline-flex items-center gap-1 font-medium whitespace-nowrap underline underline-offset-2"
        >
          View details <ArrowRight className="size-3.5" aria-hidden />
        </button>
      </div>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>{notices.length === 1 ? "Something needs a look" : "These need a look"}</DialogTitle>
          <DialogDescription>
            {technical
              ? "The system's own messages."
              : "An administrator can see the technical details under Settings > System."}
          </DialogDescription>
          <ul className="flex flex-col gap-3">
            {notices.map((n) => (
              <li key={n.key} className="flex flex-col gap-1">
                <span className="font-medium">{n.title}</span>
                <span className="text-sm text-muted-foreground">{n.detail}</span>
                {technical && (
                  <code className="rounded-md bg-muted p-2 text-xs break-words whitespace-pre-wrap">{n.technical}</code>
                )}
              </li>
            ))}
          </ul>
          {technical && (
            <Button asChild variant="outline" className="self-start">
              <Link to="/settings?tab=system">Open Settings &gt; System</Link>
            </Button>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

function Figure({ label, value, sub }: { label: string; value: string | number; sub: string }) {
  return (
    <div className="flex min-w-0 flex-1 flex-col px-4 py-3">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-3xl leading-10 font-bold whitespace-nowrap">{value}</span>
      <span className="text-sm text-muted-foreground">{sub}</span>
    </div>
  );
}

/** Present counts against what it really is out of: people rostered on a shift, or (shifts off) people with passes.
 * "Complete" only exists for calendar days, so with shifts on the second figure is On time. */
function Summary({ o }: { o: Overview }) {
  return (
    <Card className="flex divide-x rounded-2xl">
      {o.shifts ? (
        <>
          <Figure label="Present" value={`${o.present} / ${o.scheduled}`} sub="Rostered on a shift" />
          <Figure label="On time" value={o.on_time} sub="Within the grace" />
        </>
      ) : (
        <>
          <Figure label="Present" value={`${o.present} / ${o.people}`} sub="People with passes" />
          <Figure label="Complete" value={o.complete} sub="IN and OUT" />
        </>
      )}
    </Card>
  );
}

function FlowSkeleton() {
  return (
    <div role="status" aria-label="Loading" className="flex flex-col gap-6">
      <div className="grid gap-4 md:grid-cols-3">
        <Skeleton className="h-96 rounded-2xl" />
        <Skeleton className="h-96 rounded-2xl" />
        <Skeleton className="h-96 rounded-2xl" />
      </div>
      <Skeleton className="h-64 rounded-2xl" />
    </div>
  );
}

/** Decorative: the steps of the attendance record. It says nothing about where anyone is in the building. */
function Connector() {
  return (
    <div aria-hidden className="relative hidden h-px w-14 self-center bg-primary/40 lg:block">
      <span className="absolute top-1/2 left-1/2 flex size-7 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-primary/40 bg-card text-primary">
        <ChevronRight className="size-4" />
      </span>
    </div>
  );
}

function Flow({ o, isToday }: { o: Overview; isToday: boolean }) {
  return (
    <section
      aria-label="Entrance, people inside, exit"
      className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:flex lg:items-stretch lg:gap-0"
    >
      <div className="order-2 min-w-0 lg:order-none lg:flex-1">
        <CameraPanel direction="IN" cameras={o.cameras} isToday={isToday} />
      </div>
      <Connector />
      <div className="order-1 sm:col-span-2 lg:order-none lg:w-72 lg:shrink-0 2xl:w-80">
        <InsideNow count={o.inside_now} />
      </div>
      <Connector />
      <div className="order-3 min-w-0 lg:order-none lg:flex-1">
        <CameraPanel direction="OUT" cameras={o.cameras} isToday={isToday} />
      </div>
    </section>
  );
}

/** A fixed-size ring, so 0 or 1234 people shift nothing. The count is the backend's; past days have none. */
function InsideNow({ count }: { count: number | null | undefined }) {
  const { setDate } = useFilters();
  const known = count !== null && count !== undefined;
  return (
    <div className="relative flex h-full flex-col items-center justify-center gap-4">
      <div
        aria-hidden
        className="pointer-events-none absolute top-1/2 left-1/2 size-[130%] max-h-[32rem] max-w-[32rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[radial-gradient(circle,var(--glow)_0%,transparent_68%)]"
      />
      <div className="relative flex size-64 flex-col items-center justify-center rounded-full border-2 border-primary/60 bg-card/70 p-4 text-center outline-1 outline-offset-8 outline-primary/20">
        <span data-testid="inside-count" className="text-7xl leading-none font-bold tracking-tight tabular-nums">
          {known ? count : "—"}
        </span>
        <span className="mt-2 text-lg font-semibold">Inside now</span>
        <span className="text-sm text-muted-foreground">
          {known ? "People currently inside" : "Only available for today"}
        </span>
        <Users className="mt-2 size-5 text-primary" aria-hidden />
      </div>
      <div className="relative flex flex-col items-center gap-2 pt-2 text-center">
        {known ? (
          <>
            <Button asChild size="lg" className="h-12 rounded-full px-6 text-base">
              <Link to="/attendance?inside=1">
                View everyone inside <ArrowRight aria-hidden />
              </Link>
            </Button>
            <span className="text-sm text-muted-foreground">
              {count === 0
                ? "Nobody is inside right now"
                : `Search and filter ${count} ${count === 1 ? "person" : "people"}`}
            </span>
          </>
        ) : (
          <Button variant="outline" size="lg" className="h-12 rounded-full px-6" onClick={() => setDate(today())}>
            Back to today
          </Button>
        )}
      </div>
    </div>
  );
}

function stateTone(state: string): "ok" | "warn" | "error" | "neutral" {
  if (state === "running") return "ok";
  if (state === "starting") return "warn";
  return state === "not responding" ? "error" : "neutral";
}

const stateLabel = (state: string) => state.charAt(0).toUpperCase() + state.slice(1);

/** Every camera of one direction: a single one in full, several as a group, each listed. */
function CameraPanel({
  direction,
  cameras,
  isToday,
}: {
  direction: Direction;
  cameras: CameraStatus[];
  isToday: boolean;
}) {
  const { data: me } = useMe();
  const { cameras: live } = useLive();
  const title = direction === "IN" ? "Entrance" : "Exit";
  const mine = cameras
    .filter((c) => c.direction === direction)
    .map((c) => {
      const l = live[c.camera];
      return {
        ...c,
        state: l?.status ?? c.state,
        fps: l?.fps ?? c.fps,
        problem: l ? (l.error ?? l.warning) : (c.last_error ?? c.warning),
      };
    });
  const events = mine.reduce((sum, c) => sum + c.events, 0);
  const running = mine.filter((c) => c.state === "running").length;
  const only = mine.length === 1 ? mine[0] : undefined;
  return (
    <Card className="flex h-full flex-col items-center gap-3 rounded-2xl p-4 text-center">
      <div className="flex flex-col items-center gap-1">
        <h2 className="flex items-center gap-2 text-lg font-semibold">
          {title} <DirectionBadge direction={direction} />
        </h2>
        <span className="text-sm text-muted-foreground">
          {only ? only.camera : mine.length > 1 ? `${mine.length} cameras` : "Face recognition camera"}
        </span>
      </div>
      <GateIllustration direction={direction} className="max-w-56 opacity-90" />
      {mine.length === 0 ? (
        <EmptyState icon={<CameraOff />} title={`No ${title.toLowerCase()} camera`} className="py-2">
          {can(me, "cameras:manage") ? "Add one under Settings > Cameras." : "None is set up for this site."}
        </EmptyState>
      ) : (
        <>
          {only ? (
            <Badge tone={stateTone(only.state)} className="rounded-full px-3 py-1 text-sm">
              <CameraStateDot state={only.state} /> {stateLabel(only.state)}
              {only.state === "running" && only.fps != null && ` · ${only.fps.toFixed(1)} fps`}
            </Badge>
          ) : (
            <Badge
              tone={running === mine.length ? "ok" : running ? "warn" : "neutral"}
              className="rounded-full px-3 py-1 text-sm"
            >
              {running} of {mine.length} running
            </Badge>
          )}
          <span className="text-muted-foreground">
            {events} {direction} {events === 1 ? "event" : "events"} {isToday ? "today" : "this day"}
          </span>
          {only?.problem && (
            <span className="line-clamp-2 text-xs break-words text-destructive" title={only.problem}>
              {only.problem}
            </span>
          )}
          {mine.length > 1 && (
            <ul className="flex max-h-40 w-full flex-col gap-1 overflow-y-auto text-left text-sm">
              {mine.map((c) => (
                <li key={c.id} className="flex items-center gap-2" title={c.problem ?? undefined}>
                  <CameraStateDot state={c.state} />
                  <span className="min-w-0 truncate">{c.camera}</span>
                  <span className="ml-auto text-muted-foreground">
                    {c.problem ? "needs a look · " : ""}
                    {c.events}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {can(me, "live:view") && (
        <Link
          to={`/live?direction=${direction}`}
          className="mt-auto inline-flex items-center gap-1 font-medium text-primary hover:underline"
        >
          Open cameras <ArrowRight className="size-4" aria-hidden />
        </Link>
      )}
    </Card>
  );
}

function ArrivalsChart({ o }: { o: Overview }) {
  const first = o.arrivals_by_hour.findIndex((h) => h.arrivals > 0);
  const last = o.arrivals_by_hour.findLastIndex((h) => h.arrivals > 0);
  const data = o.arrivals_by_hour.slice(Math.max(0, Math.min(first, 6) - 1), Math.max(last, 18) + 2);
  const total = o.arrivals_by_hour.reduce((sum, h) => sum + h.arrivals, 0);
  return (
    <Card className="rounded-2xl">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-4">
        <h2 className="text-lg font-semibold">Arrivals per hour</h2>
        <span className="text-sm text-muted-foreground">First IN of each person</span>
      </div>
      <div className="h-56 p-2" role="img" aria-label={`Arrivals per hour: ${total} first INs on this day`}>
        {first === -1 ? (
          <EmptyState icon={<Activity />} title="No arrivals yet" className="py-8" />
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 20, right: 8, left: -16, bottom: 0 }}>
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
              <Bar
                dataKey="arrivals"
                fill="var(--primary)"
                radius={[4, 4, 0, 0]}
                maxBarSize={40}
                isAnimationActive={false}
              >
                <LabelList dataKey="arrivals" position="top" fontSize={12} fill="var(--foreground)" />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </Card>
  );
}

function recordsLink(date: string, status: Status, closedOnly = false): string {
  const query = new URLSearchParams({ from: date, to: date, status });
  if (closedOnly) query.set("open", "false");
  return `/attendance?${query}`;
}

/** The rest of a roster day's figures: each opens its records. */
function ShiftStatuses({ o }: { o: Overview }) {
  const date = o.date;
  const figures: { label: string; value: number; status?: Status }[] = [
    { label: "Late", value: o.late, status: "late" },
    { label: "Early leave", value: o.early_leave, status: "early leave" },
    { label: "Overtime", value: o.overtime },
    { label: "Not in yet", value: o.not_in_yet, status: "not in yet" },
    { label: "Absent", value: o.absent, status: "absent" },
    { label: "Off", value: o.off_day, status: "off day" },
    { label: "Unscheduled", value: o.unscheduled, status: "unscheduled" },
  ];
  return (
    <Card className="rounded-2xl p-4">
      <h2 className="mb-2 text-lg font-semibold">Shift statuses</h2>
      <ul className="grid grid-cols-2 gap-3 sm:grid-cols-4 2xl:grid-cols-7">
        {figures.map((f) => (
          <li key={f.label}>
            <Count label={f.label} value={f.value} to={f.status && recordsLink(date, f.status)} />
          </li>
        ))}
      </ul>
    </Card>
  );
}

function Count({ label, value, to }: { label: string; value: number; to?: string | undefined }) {
  const body = (
    <>
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-2xl leading-8 font-bold">{value}</span>
    </>
  );
  const box = "flex flex-col items-center rounded-lg p-1 text-center";
  return to && value > 0 ? (
    <Link to={to} className={cn(box, "hover:bg-muted")} aria-label={`${label}: ${value}, open the records`}>
      {body}
    </Link>
  ) : (
    <div className={box}>{body}</div>
  );
}

function SectionHead({ title, note }: { title: string; note?: string | undefined }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <h2 className="text-lg font-semibold">{title}</h2>
      {note && <span className="text-sm whitespace-nowrap text-muted-foreground">{note}</span>}
    </div>
  );
}

function RowsSkeleton() {
  return (
    <div role="status" aria-label="Loading" className="flex flex-col gap-2">
      {Array.from({ length: 3 }, (_, i) => (
        <Skeleton key={i} className="h-11" />
      ))}
    </div>
  );
}

function personLink(canOpen: boolean, r: AttendanceRow, date: string): string {
  return canOpen ? `/people/${r.person_id}` : `/attendance?from=${date}&to=${date}&person=${r.person_id}`;
}

/** A few of the day's people, latest arrivals first; the total is the whole day's, not this preview's. */
function Register({ date, siteId }: { date: string; siteId: number | null }) {
  const { data: me } = useMe();
  const rows = useQuery({
    queryKey: ["attendance", "overview-register", date, siteId],
    queryFn: () =>
      api.get("/api/attendance", {
        query: { from: date, to: date, site_id: siteId, page_size: PREVIEW, sort: "-first_in" },
      }),
  });
  const items = rows.data?.items ?? [];
  const total = rows.data?.total;
  const title = date === today() ? "Today's register" : "The day's register";
  return (
    <section aria-label={title} className="flex flex-col gap-2">
      <SectionHead
        title={title}
        note={total === undefined ? undefined : `${total} ${total === 1 ? "person" : "people"}`}
      />
      {rows.error ? (
        <ErrorState error={rows.error} retry={() => void rows.refetch()} />
      ) : rows.isPending ? (
        <RowsSkeleton />
      ) : items.length === 0 ? (
        <EmptyState icon={<Users />} title="Nobody yet" className="py-4">
          People appear here once they pass a camera or are on the roster.
        </EmptyState>
      ) : (
        <>
          <ul className="flex flex-col divide-y">
            {items.map((r) => (
              <li key={`${r.person_id}-${r.date}-${r.shift_id ?? ""}`}>
                <Link
                  to={personLink(can(me, "people:read"), r, date)}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md py-2 hover:bg-muted"
                >
                  <Avatar name={r.name} />
                  <span className="flex min-w-0 flex-1 basis-32 flex-col">
                    <span className="truncate font-medium">{r.name}</span>
                    <span className="truncate text-sm text-muted-foreground">{r.employee_code}</span>
                  </span>
                  <span className="text-sm whitespace-nowrap text-muted-foreground">
                    {r.first_in ? `In ${timeOn(r.first_in, r.date)}` : "No IN"}
                    {r.last_out && ` · Out ${timeOn(r.last_out, r.date)}`}
                  </span>
                  <StatusBadge status={r.status} inside={r.open && r.inside} />
                </Link>
              </li>
            ))}
          </ul>
          <Link
            to={`/attendance?from=${date}&to=${date}`}
            className="inline-flex items-center gap-1 self-end font-medium text-primary hover:underline"
          >
            View full register <ArrowRight className="size-4" aria-hidden />
          </Link>
        </>
      )}
    </section>
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

/** The newest of the day's events: from the API, plus those arriving live (the same event is shown once). */
function RecentEvents({ date, siteId }: { date: string; siteId: number | null }) {
  const { events: live } = useLive();
  const recent = useQuery({
    queryKey: ["events", "overview", date, siteId],
    queryFn: () => api.get("/api/events", { query: { from: date, to: date, site_id: siteId, page_size: PREVIEW } }),
  });
  const liveIds = new Set(live.map((e) => e.id));
  const fromLive = live.filter((e) => e.timestamp.startsWith(date) && (siteId === null || e.site_id === siteId));
  const merged = [...fromLive, ...(recent.data?.items ?? []).filter((e) => !liveIds.has(e.id))]
    .sort((a, b) => b.timestamp.localeCompare(a.timestamp))
    .slice(0, PREVIEW);
  const total = recent.data ? Math.max(recent.data.total, merged.length) : undefined;
  return (
    <section aria-label="Recent events" className="flex flex-col gap-2">
      <SectionHead title="Recent events" note={total === undefined ? undefined : `${merged.length} of ${total}`} />
      {recent.error ? (
        <ErrorState error={recent.error} retry={() => void recent.refetch()} />
      ) : recent.isPending ? (
        <RowsSkeleton />
      ) : merged.length === 0 ? (
        <EmptyState icon={<Inbox />} title="No events yet" className="py-4">
          They appear here as people pass the cameras.
        </EmptyState>
      ) : (
        <ul aria-label="Events, newest first" aria-live="polite" className="flex flex-col gap-3">
          {merged.map((e) => (
            <li key={e.id} className="flex items-center gap-3">
              <span className="w-11 shrink-0 text-sm font-medium">{hhmm(e.timestamp)}</span>
              <Avatar name={e.name} src={e.snapshot_url} />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate font-medium">{e.name}</span>
                <span className="truncate text-sm text-muted-foreground">
                  {e.employee_code} · {e.camera}
                  {e.manual && " · manual"}
                  {e.voided && " · voided"}
                </span>
              </span>
              <DirectionBadge direction={e.direction} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** The backend's counts for the whole day. "All clear" only with an answer from the server, and every count at 0. */
function Attention({ o, failed, date }: { o: Overview | undefined; failed: boolean; date: string }) {
  if (!o) {
    return failed ? (
      <p className="text-sm text-muted-foreground">Attendance issues can't be shown: the server didn't answer.</p>
    ) : (
      <Skeleton className="h-24" />
    );
  }
  const clear = !failed && o.missing_in === 0 && o.missing_out === 0 && o.check === 0;
  return (
    <section aria-label="Needs attention" className="flex flex-col gap-3">
      {clear ? (
        <div className="flex items-center gap-3 rounded-xl bg-accent p-3">
          <CircleCheck className="size-6 shrink-0 text-primary" aria-hidden />
          <span className="flex flex-col">
            <span className="font-medium">No attendance issues</span>
            <span className="text-sm text-muted-foreground">No missing IN, missing OUT or check on this day.</span>
          </span>
        </div>
      ) : (
        <h2 className="text-lg font-semibold">Needs attention</h2>
      )}
      <ul className="grid grid-cols-3 divide-x">
        <li>
          <Count label="Missing IN" value={o.missing_in} to={recordsLink(date, "missing IN")} />
        </li>
        <li>
          {/* Closed days only, as the count: a calendar day still running isn't missing its OUT yet. */}
          <Count label="Missing OUT" value={o.missing_out} to={recordsLink(date, "missing OUT", true)} />
        </li>
        <li>
          <Count label="Check" value={o.check} to={recordsLink(date, "check")} />
        </li>
      </ul>
    </section>
  );
}
