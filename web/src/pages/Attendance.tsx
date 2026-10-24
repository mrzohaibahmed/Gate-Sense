import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ClipboardList, Ban, RotateCcw, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { toast } from "sonner";

import { api, resourceUrl } from "@/api/client";
import type { AttendanceEvent, AttendanceRow, Status } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { useFilters } from "@/app/filters";
import { can, useMe } from "@/auth/auth";
import { DirectionBadge, StatusBadge } from "@/components/badges";
import { type ColumnMeta, DataTable } from "@/components/DataTable";
import { ReasonDialog } from "@/components/ReasonDialog";
import { Button } from "@/components/ui/button";
import { Badge, Separator } from "@/components/ui/card";
import { Dialog, DialogDescription, DialogTitle, SheetContent } from "@/components/ui/dialog";
import { Input, NativeSelect } from "@/components/ui/input";
import { EmptyState, ErrorState, Switch } from "@/components/ui/misc";
import { cn, duration, hhmm, today } from "@/lib/utils";

import { CorrectionForm } from "./CorrectionForm";

export const STATUSES: Status[] = ["complete", "missing exit", "missing entry", "check", "absent"];
const STATUS_OPTION: Record<Status, string> = {
  complete: "Complete (in and out)",
  "missing exit": "Missing exit",
  "missing entry": "Missing entry",
  check: "Check (last OUT before first IN)",
  absent: "Absent",
};
const PAGE_SIZE = 100;

function columns(isToday: boolean): ColumnDef<AttendanceRow>[] {
  const meta = (m: ColumnMeta) => m;
  return [
    { accessorKey: "date", header: "Date", meta: meta({ label: "Date", sortKey: "date" }) },
    {
      accessorKey: "name",
      header: "Name",
      enableHiding: false,
      meta: meta({ label: "Name", sortKey: "name" }),
      cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
    },
    { accessorKey: "employee_code", header: "Code", meta: meta({ label: "Code", sortKey: "employee_code" }) },
    {
      accessorKey: "department",
      header: "Department",
      meta: meta({ label: "Department", sortKey: "department" }),
      cell: ({ getValue }) => (getValue() as string | null) ?? "",
    },
    {
      accessorKey: "first_in",
      header: "First IN",
      meta: meta({ label: "First IN", sortKey: "first_in" }),
      cell: ({ getValue }) => hhmm(getValue() as string | null),
    },
    {
      accessorKey: "last_out",
      header: "Last OUT",
      meta: meta({ label: "Last OUT", sortKey: "last_out" }),
      cell: ({ getValue }) => hhmm(getValue() as string | null),
    },
    {
      accessorKey: "worked_minutes",
      header: "Worked",
      meta: meta({ label: "Worked", sortKey: "worked_hours" }),
      cell: ({ getValue }) => duration(getValue() as number | null),
    },
    {
      accessorKey: "status",
      header: "Status",
      meta: meta({ label: "Status", sortKey: "status" }),
      cell: ({ row }) => <StatusBadge status={row.original.status} today={isToday && row.original.date === today()} />,
    },
  ];
}

export function AttendancePage() {
  const { date, siteId } = useFilters();
  const { data: me } = useMe();
  const [params, setParams] = useSearchParams();
  const from = params.get("from") ?? date;
  const to = params.get("to") ?? from;
  const status = (params.get("status") as Status | null) ?? null;
  const department = params.get("department") ?? "";
  const person = params.get("person");
  const q = params.get("q") ?? "";
  const sort = params.get("sort") ?? "-date";
  const page = Number(params.get("page") ?? 1);
  const [search, setSearch] = useState(q);
  const [selected, setSelected] = useState<AttendanceRow | null>(null);

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    if (!("page" in changes)) next.delete("page");
    setParams(next, { replace: true });
  };

  const rows = useQuery({
    queryKey: ["attendance", "table", { from, to, status, department, person, q, sort, page, siteId }],
    queryFn: () =>
      api.get("/api/attendance", {
        query: {
          from,
          to,
          status,
          department: department || null,
          person: person ? Number(person) : null,
          q: q || null,
          sort,
          page,
          page_size: PAGE_SIZE,
          site_id: siteId,
        },
      }),
    placeholderData: keepPreviousData,
  });
  const people = useQuery({
    queryKey: ["people", "departments"],
    queryFn: () => api.get("/api/people", { query: { page_size: 500 } }),
    enabled: can(me, "people:read"),
    staleTime: 300_000,
  });
  const departments = useMemo(() => {
    const names = new Set<string>();
    for (const p of people.data?.items ?? []) if (p.department) names.add(p.department);
    for (const r of rows.data?.items ?? []) if (r.department) names.add(r.department);
    return [...names].sort();
  }, [people.data, rows.data]);
  const cols = useMemo(() => columns(to === today()), [to]);

  return (
    <>
      <PageHeader title="Attendance" />
      <div className="px-4 pb-4">
        <DataTable
          columns={cols}
          data={rows.data?.items}
          total={rows.data?.total}
          page={page}
          pageSize={PAGE_SIZE}
          onPage={(p) => update({ page: String(p) })}
          sort={sort}
          onSort={(s) => update({ sort: s })}
          isLoading={rows.isPending}
          error={rows.error}
          retry={() => void rows.refetch()}
          onRowClick={setSelected}
          rowLabel={(r) => `${r.name}, ${r.date}`}
          initialHidden={from === to ? ["date"] : []}
          empty={
            <EmptyState icon={<ClipboardList />} title="No rows">
              Nobody matches these filters.
            </EmptyState>
          }
          toolbar={
            <>
              <label className="flex items-center gap-1 text-xs text-muted-foreground">
                From
                <input
                  type="date"
                  aria-label="From"
                  value={from}
                  max={to}
                  onChange={(e) => e.target.value && update({ from: e.target.value })}
                  className="h-8 rounded-md border border-input bg-card px-2 text-sm text-foreground"
                />
              </label>
              <label className="flex items-center gap-1 text-xs text-muted-foreground">
                To
                <input
                  type="date"
                  aria-label="To"
                  value={to}
                  min={from}
                  max={today()}
                  onChange={(e) => e.target.value && update({ to: e.target.value })}
                  className="h-8 rounded-md border border-input bg-card px-2 text-sm text-foreground"
                />
              </label>
              <NativeSelect
                aria-label="Department"
                value={department}
                onChange={(e) => update({ department: e.target.value })}
              >
                <option value="">All departments</option>
                {departments.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </NativeSelect>
              <NativeSelect
                aria-label="Status"
                value={status ?? ""}
                onChange={(e) => update({ status: e.target.value })}
              >
                <option value="">All statuses</option>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {STATUS_OPTION[s]}
                  </option>
                ))}
              </NativeSelect>
              <form
                role="search"
                onSubmit={(e) => {
                  e.preventDefault();
                  update({ q: search.trim() });
                }}
                className="relative"
              >
                <Search className="absolute top-2 left-2 size-4 text-muted-foreground" aria-hidden />
                <Input
                  aria-label="Search name or code"
                  placeholder="Name or code"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  onBlur={() => search.trim() !== q && update({ q: search.trim() })}
                  className="w-44 pl-7"
                />
              </form>
              {person && (
                <Badge tone="accent">
                  One person
                  <button type="button" onClick={() => update({ person: null })} aria-label="Show everyone">
                    ×
                  </button>
                </Badge>
              )}
            </>
          }
        />
      </div>
      <Dialog open={selected !== null} onOpenChange={(o) => !o && setSelected(null)}>
        <SheetContent aria-describedby={undefined}>{selected && <DayPanel row={selected} />}</SheetContent>
      </Dialog>
    </>
  );
}

/** One person's day: raw events with snapshots, void/restore, and the correction form (HR and admin). */
export function DayPanel({ row }: { row: AttendanceRow }) {
  const { data: me } = useMe();
  const queryClient = useQueryClient();
  const correct = can(me, "attendance:correct");
  const [showVoided, setShowVoided] = useState(true);
  const [target, setTarget] = useState<AttendanceEvent | null>(null);
  const events = useQuery({
    queryKey: ["events", "day", row.person_id, row.date],
    queryFn: () =>
      api.get("/api/events", {
        query: { person: row.person_id, from: row.date, to: row.date, sort: "timestamp", page_size: 200 },
      }),
  });
  const items = events.data?.items ?? [];
  const voidedCount = items.filter((e) => e.voided).length;
  const shown = showVoided ? items : items.filter((e) => !e.voided);
  const toggle = async (e: AttendanceEvent, reason: string) => {
    await api.post("/api/attendance/corrections", {
      body: { action: e.voided ? "restore" : "void", event_id: e.id, reason },
    });
    toast.success(
      e.voided ? `${e.direction} at ${hhmm(e.timestamp)} restored` : `${e.direction} at ${hhmm(e.timestamp)} voided`,
    );
    for (const key of [["attendance"], ["events"], ["overview"], ["person"]]) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  };
  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex flex-col gap-1 pr-6">
        <DialogTitle>{row.name}</DialogTitle>
        <DialogDescription>
          {row.employee_code}
          {row.department ? ` · ${row.department}` : ""} · {row.date}
        </DialogDescription>
        <div className="flex flex-wrap items-center gap-2 pt-1 text-sm">
          <StatusBadge status={row.status} today={row.date === today()} />
          <span>
            IN {hhmm(row.first_in) || "—"} · OUT {hhmm(row.last_out) || "—"}
            {row.worked_minutes != null && ` · ${duration(row.worked_minutes)}`}
          </span>
        </div>
      </div>
      <Separator />
      <section aria-label="Events" className="flex flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-semibold">Events</h3>
          {voidedCount > 0 && (
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <Switch checked={showVoided} onCheckedChange={setShowVoided} aria-label="Show voided events" />
              Show voided ({voidedCount})
            </label>
          )}
        </div>
        {events.error ? (
          <ErrorState error={events.error} retry={() => void events.refetch()} />
        ) : events.isPending ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : shown.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {items.length === 0 ? "No events that day." : "Only voided events that day."}
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {shown.map((e) => (
              <li
                key={e.id}
                className={cn(
                  "flex items-center gap-2 rounded-md border p-2",
                  (e.voided || e.repeat) && "bg-muted/50 opacity-60",
                )}
                aria-label={`${e.direction} at ${hhmm(e.timestamp)}${e.voided ? " (voided)" : e.repeat ? " (repeat)" : ""}`}
              >
                {e.snapshot_url ? (
                  <a href={resourceUrl(e.snapshot_url)} target="_blank" rel="noreferrer">
                    <img
                      src={resourceUrl(e.snapshot_url)}
                      alt={`Snapshot at ${hhmm(e.timestamp)}`}
                      className="size-12 rounded object-cover"
                    />
                  </a>
                ) : (
                  <span className="flex size-12 items-center justify-center rounded bg-muted text-xs text-muted-foreground">
                    {e.manual ? "manual" : "—"}
                  </span>
                )}
                <div className="flex flex-col">
                  <span className="flex items-center gap-2">
                    <DirectionBadge direction={e.direction} />
                    <span className={e.voided ? "line-through" : "font-medium"}>{hhmm(e.timestamp)}</span>
                    {e.original_timestamp && (
                      <span className="text-xs text-muted-foreground">was {hhmm(e.original_timestamp)}</span>
                    )}
                    {e.voided && <Badge tone="error">Voided</Badge>}
                    {e.repeat && <Badge title="Kept, but the day uses the first IN and the last OUT">repeat</Badge>}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {e.camera}
                    {e.score != null && ` · score ${e.score.toFixed(2)}`}
                    {e.manual && " · manual"}
                  </span>
                </div>
                {correct && (
                  <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setTarget(e)}>
                    {e.voided ? <RotateCcw /> : <Ban />}
                    {e.voided ? "Restore" : "Void"}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
      {can(me, "attendance:correct") && (
        <>
          <Separator />
          <section className="flex flex-col gap-2">
            <h3 className="text-sm font-semibold">Add correction</h3>
            <CorrectionForm personId={row.person_id} date={row.date} events={items} />
          </section>
        </>
      )}
      <ReasonDialog
        open={target !== null}
        onOpenChange={(o) => !o && setTarget(null)}
        title={target?.voided ? "Restore this event?" : "Void this event?"}
        description={
          target
            ? target.voided
              ? `The ${target.direction} at ${hhmm(target.timestamp)} counts again.`
              : `The ${target.direction} at ${hhmm(target.timestamp)} stops counting. Nothing is deleted; you can restore it.`
            : undefined
        }
        confirmLabel={target?.voided ? "Restore" : "Void"}
        destructive={!target?.voided}
        onConfirm={(reason) => (target ? toggle(target, reason) : Promise.resolve())}
      />
    </div>
  );
}
