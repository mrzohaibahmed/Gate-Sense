import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarDays, ChevronLeft, ChevronRight, Copy, FileSpreadsheet, Save, Search, Undo2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { toast } from "sonner";

import { api } from "@/api/client";
import type { RosterEntry, RosterRow, Shift } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { useFilters } from "@/app/filters";
import { useShiftModeKnown, useShiftsEnabled } from "@/app/shiftMode";
import { can, useMe } from "@/auth/auth";
import { ReasonDialog } from "@/components/ReasonDialog";
import { TableSkeleton } from "@/components/states";
import { Badge, Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { EmptyState, ErrorState, Td, Th } from "@/components/ui/misc";
import { addDays, mondayOf } from "@/lib/days";
import { cn, today } from "@/lib/utils";

import { RosterImportDialog } from "./RosterImport";

/** A cell's value: "" = no entry (unscheduled), "OFF", or a shift id as text. */
type Value = string;
const OFF = "OFF";
const DAY = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric" });

function key(personId: number, date: string): string {
  return `${personId}|${date}`;
}

function valueOf(row: RosterRow, date: string): Value {
  const cell = row.cells.find((c) => c.date === date);
  if (!cell) return "";
  return cell.shift_id === null ? OFF : String(cell.shift_id);
}

function dayLabel(date: string): string {
  const [y, m, d] = date.split("-").map(Number) as [number, number, number];
  return DAY.format(new Date(y, m - 1, d));
}

/**
 * The roster: who works which shift on each day of a week (OFF = day off; an empty cell = not planned). Admin and HR
 * change cells (one at a time, or for selected people and days at once, or by copying last week), review them, and
 * save them together with one reason; every change is audited. Attendance is recalculated from the camera passes.
 */
export function RosterPage() {
  const shifts = useShiftsEnabled();
  const known = useShiftModeKnown();
  if (shifts) return <RosterWeek />;
  if (!known) return <TableSkeleton rows={6} />;
  return (
    <>
      <PageHeader title="Roster" />
      <div className="px-4 pb-4">
        <EmptyState icon={<CalendarDays />} title="Shifts are switched off">
          Attendance is by calendar day. The roster is kept; an admin can switch shifts on in Settings &gt; System.
        </EmptyState>
      </div>
    </>
  );
}

function RosterWeek() {
  const { siteId } = useFilters();
  const { data: me } = useMe();
  const queryClient = useQueryClient();
  const editable = can(me, "roster:manage");
  const [params, setParams] = useSearchParams();
  const week = mondayOf(params.get("week") ?? today());
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => addDays(week, i)), [week]);
  const department = params.get("department") ?? "";
  const [search, setSearch] = useState(params.get("q") ?? "");
  const q = params.get("q") ?? "";
  const [staged, setStaged] = useState<Map<string, Value>>(new Map());
  const [people, setPeople] = useState<Set<number>>(new Set());
  const [selectedDays, setSelectedDays] = useState<Set<string>>(new Set());
  const [bulk, setBulk] = useState<Value>("");
  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);

  const update = (changes: Record<string, string>) =>
    setParams(
      (p) => {
        const next = new URLSearchParams(p);
        for (const [k, v] of Object.entries(changes)) {
          if (v) next.set(k, v);
          else next.delete(k);
        }
        return next;
      },
      { replace: true },
    );
  const filters = { site_id: siteId, department: department || null, q: q || null };
  const shifts = useQuery({
    queryKey: ["shifts", "on", week],
    queryFn: () => api.get("/api/shifts", { query: { on: week, page_size: 500 } }),
  });
  const roster = useQuery({
    queryKey: ["roster", week, filters],
    queryFn: () =>
      api.get("/api/roster", { query: { from: week, to: days[6], page_size: 500, sort: "name", ...filters } }),
  });
  const departments = useQuery({
    queryKey: ["people", "departments"],
    queryFn: () => api.get("/api/people", { query: { page_size: 500 } }),
    enabled: can(me, "people:read"),
    staleTime: 300_000,
  });
  const departmentNames = useMemo(() => {
    const names = new Set<string>();
    for (const p of departments.data?.items ?? []) if (p.department) names.add(p.department);
    for (const r of roster.data?.items ?? []) if (r.department) names.add(r.department);
    return [...names].sort();
  }, [departments.data, roster.data]);

  const rows = roster.data?.items ?? [];
  const shiftList = shifts.data?.items ?? [];
  const byId = new Map(shiftList.map((s) => [String(s.id), s]));
  const current = (row: RosterRow, date: string) => valueOf(row, date);
  const shown = (row: RosterRow, date: string) => staged.get(key(row.person_id, date)) ?? current(row, date);

  /** Stage a value; staging a cell back to what is saved drops the change. */
  const stage = (changes: [RosterRow, string, Value][]) =>
    setStaged((old) => {
      const next = new Map(old);
      for (const [row, date, value] of changes) {
        if (value === current(row, date)) next.delete(key(row.person_id, date));
        else next.set(key(row.person_id, date), value);
      }
      return next;
    });
  const moveWeek = (to: string) => {
    if (staged.size && !window.confirm("Leave this week without saving the changes?")) return;
    setStaged(new Map());
    setPeople(new Set());
    setSelectedDays(new Set());
    update({ week: to });
  };

  const applyBulk = () => {
    const changes: [RosterRow, string, Value][] = [];
    for (const row of rows) {
      if (!people.has(row.person_id)) continue;
      for (const date of days) if (selectedDays.has(date)) changes.push([row, date, bulk]);
    }
    stage(changes);
    toast.success(`${changes.length} cells set to ${label(bulk, byId)}; review and save.`);
  };
  const copyLastWeek = async () => {
    const last = await api.get("/api/roster", {
      query: { from: addDays(week, -7), to: addDays(week, -1), page_size: 500, ...filters },
    });
    const previous = new Map(last.items.map((r) => [r.person_id, r]));
    const changes: [RosterRow, string, Value][] = [];
    for (const row of rows) {
      const before = previous.get(row.person_id);
      if (!before) continue;
      for (const date of days) {
        const value = valueOf(before, addDays(date, -7));
        if (value !== "") changes.push([row, date, value]);
      }
    }
    stage(changes);
    toast.success(
      changes.length
        ? `Copied ${changes.length} entries from last week; review and save.`
        : "Last week has no entries for these people.",
    );
  };
  const save = async (reason: string) => {
    const entries: RosterEntry[] = [...staged].map(([k, value]) => {
      const [person, date] = k.split("|") as [string, string];
      return {
        person_id: Number(person),
        date,
        shift_id: value === "" || value === OFF ? null : Number(value),
        clear: value === "",
      };
    });
    const done = await api.put("/api/roster", { body: { entries, reason } });
    toast.success(`Roster saved: ${done.changed} set, ${done.removed} removed.`);
    setStaged(new Map());
    for (const k of [["roster"], ["attendance"], ["overview"], ["reports"], ["shifts"]]) {
      void queryClient.invalidateQueries({ queryKey: k });
    }
  };

  const allPeople = rows.length > 0 && rows.every((r) => people.has(r.person_id));
  const toggle = <T,>(set: Set<T>, item: T) => {
    const next = new Set(set);
    if (next.has(item)) next.delete(item);
    else next.add(item);
    return next;
  };

  return (
    <>
      <PageHeader title="Roster">
        {editable && (
          <>
            <Button variant="outline" size="sm" onClick={() => setImporting(true)}>
              <FileSpreadsheet /> Import from Excel
            </Button>
            <Button variant="outline" size="sm" onClick={() => void copyLastWeek()} disabled={!rows.length}>
              <Copy /> Copy last week
            </Button>
          </>
        )}
      </PageHeader>
      <div className="flex flex-col gap-4 px-4 pb-4">
        <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Week and filters">
          <Button variant="outline" size="sm" aria-label="Previous week" onClick={() => moveWeek(addDays(week, -7))}>
            <ChevronLeft />
          </Button>
          <input
            type="date"
            aria-label="Week of"
            value={week}
            onChange={(e) => e.target.value && moveWeek(mondayOf(e.target.value))}
            className="h-8 rounded-md border border-input bg-card px-2 text-sm text-foreground"
          />
          <Button variant="outline" size="sm" aria-label="Next week" onClick={() => moveWeek(addDays(week, 7))}>
            <ChevronRight />
          </Button>
          <Button variant="ghost" size="sm" onClick={() => moveWeek(mondayOf(today()))}>
            This week
          </Button>
          <NativeSelect
            aria-label="Department"
            value={department}
            onChange={(e) => update({ department: e.target.value })}
          >
            <option value="">All departments</option>
            {departmentNames.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </NativeSelect>
          <form
            role="search"
            className="relative"
            onSubmit={(e) => {
              e.preventDefault();
              update({ q: search.trim() });
            }}
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
        </div>
        <ShiftLegend shifts={shiftList} />
        {editable && (
          <div
            className="flex flex-wrap items-center gap-2 rounded-md border bg-card px-3 py-2 text-sm"
            aria-label="Set selected cells"
          >
            <span className="text-muted-foreground">
              Selected: {people.size} {people.size === 1 ? "person" : "people"} × {selectedDays.size}{" "}
              {selectedDays.size === 1 ? "day" : "days"}
            </span>
            <NativeSelect
              aria-label="Value for the selected cells"
              value={bulk}
              onChange={(e) => setBulk(e.target.value)}
            >
              <ValueOptions shifts={shiftList} empty="— (remove)" />
            </NativeSelect>
            <Button size="sm" variant="outline" disabled={!people.size || !selectedDays.size} onClick={applyBulk}>
              Apply to selected
            </Button>
            <span className="ml-auto flex items-center gap-2">
              {staged.size > 0 && <Badge tone="warn">{staged.size} unsaved</Badge>}
              <Button size="sm" variant="ghost" disabled={!staged.size} onClick={() => setStaged(new Map())}>
                <Undo2 /> Discard
              </Button>
              <Button size="sm" disabled={!staged.size} onClick={() => setSaving(true)}>
                <Save /> Save changes
              </Button>
            </span>
          </div>
        )}
        <Card>
          <CardHeader>
            <CardTitle>
              Week of {week} to {days[6]}
            </CardTitle>
            <span className="text-xs text-muted-foreground">
              OFF = day off (never absent). An empty cell is not planned: passes that day are unscheduled.
            </span>
          </CardHeader>
          {roster.error || shifts.error ? (
            <ErrorState error={roster.error ?? shifts.error} retry={() => void roster.refetch()} />
          ) : roster.isPending || shifts.isPending ? (
            <TableSkeleton rows={6} />
          ) : rows.length === 0 ? (
            <EmptyState icon={<CalendarDays />} title="Nobody here">
              No active people match these filters.
            </EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" aria-label="Roster">
                <thead>
                  <tr>
                    <Th className="w-56">
                      {editable ? (
                        <label className="flex items-center gap-2">
                          <input
                            type="checkbox"
                            aria-label="Select everyone"
                            checked={allPeople}
                            onChange={() => setPeople(allPeople ? new Set() : new Set(rows.map((r) => r.person_id)))}
                          />
                          Person
                        </label>
                      ) : (
                        "Person"
                      )}
                    </Th>
                    {days.map((date) => (
                      <Th key={date} className={cn("min-w-28", date === today() && "text-primary")}>
                        {editable ? (
                          <label className="flex items-center gap-1">
                            <input
                              type="checkbox"
                              aria-label={`Select ${date}`}
                              checked={selectedDays.has(date)}
                              onChange={() => setSelectedDays((s) => toggle(s, date))}
                            />
                            {dayLabel(date)}
                          </label>
                        ) : (
                          dayLabel(date)
                        )}
                      </Th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.person_id} aria-label={row.name}>
                      <Td>
                        <label className="flex items-center gap-2">
                          {editable && (
                            <input
                              type="checkbox"
                              aria-label={`Select ${row.name}`}
                              checked={people.has(row.person_id)}
                              onChange={() => setPeople((s) => toggle(s, row.person_id))}
                            />
                          )}
                          <span className="flex flex-col">
                            <span className="font-medium">
                              {row.name}
                              {!row.active && (
                                <Badge className="ml-1" tone="neutral">
                                  inactive
                                </Badge>
                              )}
                            </span>
                            <span className="text-xs text-muted-foreground">
                              {row.employee_code}
                              {row.department ? ` · ${row.department}` : ""}
                            </span>
                          </span>
                        </label>
                      </Td>
                      {days.map((date) => {
                        const value = shown(row, date);
                        const changed = staged.has(key(row.person_id, date));
                        return (
                          <Td key={date} className={cn(changed && "bg-warn/10")}>
                            {editable ? (
                              <NativeSelect
                                aria-label={`${row.name} on ${date}`}
                                value={value}
                                onChange={(e) => stage([[row, date, e.target.value]])}
                                className={cn("w-full", value === OFF && "text-muted-foreground")}
                              >
                                <ValueOptions shifts={shiftList} empty="—" />
                              </NativeSelect>
                            ) : (
                              <span className={cn(value === OFF && "text-muted-foreground")}>{label(value, byId)}</span>
                            )}
                          </Td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
      <ReasonDialog
        open={saving}
        onOpenChange={setSaving}
        title={`Save ${staged.size} roster ${staged.size === 1 ? "change" : "changes"}?`}
        description="Attendance for these days is worked out again from the camera passes. Every change is audited."
        confirmLabel="Save"
        onConfirm={save}
      />
      <RosterImportDialog open={importing} onOpenChange={setImporting} first={week} last={days[6]!} />
    </>
  );
}

function label(value: Value, shifts: Map<string, Shift>): string {
  if (value === "") return "—";
  if (value === OFF) return "OFF";
  return shifts.get(value)?.name ?? "?";
}

function ValueOptions({ shifts, empty }: { shifts: Shift[]; empty: string }) {
  return (
    <>
      <option value="">{empty}</option>
      {shifts.map((s) => (
        <option key={s.id} value={String(s.id)}>
          {s.name}
        </option>
      ))}
      <option value={OFF}>OFF</option>
    </>
  );
}

function ShiftLegend({ shifts }: { shifts: Shift[] }) {
  if (!shifts.length) return null;
  return (
    <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label="Shifts">
      {shifts.map((s) => (
        <span key={s.id}>
          <span className="font-medium text-foreground">{s.name}</span> {s.start}–{s.end}
          {s.crosses_midnight ? " (next day)" : ""}, grace {s.grace_min} min
        </span>
      ))}
    </p>
  );
}
