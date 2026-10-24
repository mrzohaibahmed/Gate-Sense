import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { FileSpreadsheet, FileText, Inbox } from "lucide-react";
import { useMemo } from "react";
import { useSearchParams } from "react-router";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts";

import { api, resourceUrl } from "@/api/client";
import type { DailyRow, PersonTotal } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { useFilters } from "@/app/filters";
import { can, useMe } from "@/auth/auth";
import { StatusBadge } from "@/components/badges";
import { TableSkeleton } from "@/components/states";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/input";
import { EmptyState, ErrorState, Table, Td, Th } from "@/components/ui/misc";
import { type Preset, presetRange } from "@/lib/ranges";
import { hhmm, today } from "@/lib/utils";

type Kind = "daily" | "monthly";
const PRESETS: [Preset, string][] = [
  ["today", "Today"],
  ["week", "This week"],
  ["month", "This month"],
  ["last-month", "Last month"],
  ["custom", "Custom"],
];
/** Rows shown in the preview; the export always has every row. */
const PREVIEW_ROWS = 500;

function hours(h: number | null | undefined): string {
  return h == null ? "" : h.toFixed(2);
}

export function ReportsPage() {
  const { siteId } = useFilters();
  const { data: me } = useMe();
  const [params, setParams] = useSearchParams();
  const kind: Kind = params.get("kind") === "monthly" ? "monthly" : "daily";
  const preset = (params.get("range") as Preset | null) ?? (kind === "monthly" ? "month" : "today");
  const [defaultFrom, defaultTo] = presetRange(preset === "custom" ? "today" : preset);
  const from = preset === "custom" ? (params.get("from") ?? defaultFrom) : defaultFrom;
  const to = preset === "custom" ? (params.get("to") ?? defaultTo) : defaultTo;
  const department = params.get("department") ?? "";
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
  const query = { from, to, department: department || null, site_id: siteId };

  const report = useQuery({
    queryKey: ["reports", kind, query],
    queryFn: async (): Promise<{ daily?: DailyRow[]; monthly?: PersonTotal[] }> =>
      kind === "daily"
        ? { daily: (await api.get("/api/reports/daily", { query })).rows }
        : { monthly: (await api.get("/api/reports/monthly", { query })).people },
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
    for (const r of [...(report.data?.daily ?? []), ...(report.data?.monthly ?? [])]) {
      if (r.department) names.add(r.department);
    }
    return [...names].sort();
  }, [people.data, report.data]);
  const exportUrl = (format: "csv" | "xlsx") =>
    resourceUrl("/api/reports/{kind}/export", { kind }, { ...query, format });

  return (
    <>
      <PageHeader title="Reports">
        <Button asChild variant="outline" size="sm">
          <a href={exportUrl("csv")} download>
            <FileText /> Export CSV
          </a>
        </Button>
        <Button asChild size="sm">
          <a href={exportUrl("xlsx")} download>
            <FileSpreadsheet /> Export Excel
          </a>
        </Button>
      </PageHeader>
      <div className="flex flex-col gap-4 px-4 pb-4">
        <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Report filters">
          <div role="tablist" aria-label="Report" className="flex gap-1 rounded-md bg-muted p-1 text-sm">
            {(
              [
                ["daily", "Daily register"],
                ["monthly", "Monthly summary"],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                type="button"
                role="tab"
                aria-selected={kind === k}
                onClick={() => update({ kind: k, range: "" })}
                className={kind === k ? "rounded bg-background px-2 py-1 font-medium shadow-sm" : "rounded px-2 py-1"}
              >
                {label}
              </button>
            ))}
          </div>
          <NativeSelect
            aria-label="Range"
            value={preset}
            onChange={(e) =>
              update(e.target.value === "custom" ? { range: "custom", from, to } : { range: e.target.value })
            }
          >
            {PRESETS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </NativeSelect>
          <input
            type="date"
            aria-label="From"
            value={from}
            max={to}
            onChange={(e) => e.target.value && update({ range: "custom", from: e.target.value, to })}
            className="h-8 rounded-md border border-input bg-card px-2 text-sm text-foreground"
          />
          <span className="text-sm text-muted-foreground">to</span>
          <input
            type="date"
            aria-label="To"
            value={to}
            min={from}
            max={today()}
            onChange={(e) => e.target.value && update({ range: "custom", from, to: e.target.value })}
            className="h-8 rounded-md border border-input bg-card px-2 text-sm text-foreground"
          />
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
        </div>
        <p className="text-xs text-muted-foreground">
          A working day runs from the first IN to the last OUT of the calendar date. Voided events don't count;
          corrected times do.
        </p>
        {report.error ? (
          <ErrorState error={report.error} retry={() => void report.refetch()} />
        ) : report.isPending ? (
          <Card>
            <TableSkeleton rows={6} />
          </Card>
        ) : report.data.daily ? (
          <DailyTable rows={report.data.daily} />
        ) : (
          <MonthlyView people={report.data.monthly ?? []} />
        )}
      </div>
    </>
  );
}

function Summary({ items }: { items: [string, string | number][] }) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {items.map(([label, value]) => (
        <Card key={label}>
          <CardContent className="flex flex-col gap-1 py-3">
            <span className="text-xs text-muted-foreground">{label}</span>
            <span className="text-xl font-semibold tabular-nums">{value}</span>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function DailyTable({ rows }: { rows: DailyRow[] }) {
  const worked = rows.reduce((sum, r) => sum + (r.worked_hours ?? 0), 0);
  return (
    <>
      <Summary
        items={[
          ["Person-days", rows.length],
          ["Present", rows.filter((r) => r.first_in).length],
          ["Missing OUT", rows.filter((r) => r.missing_out).length],
          ["To check", rows.filter((r) => r.check).length],
          ["Hours worked", worked.toFixed(1)],
        ]}
      />
      <Card>
        <CardHeader>
          <CardTitle>Daily register</CardTitle>
          {rows.length > PREVIEW_ROWS && (
            <span className="text-xs text-muted-foreground">
              Showing {PREVIEW_ROWS} of {rows.length} rows; the export has them all.
            </span>
          )}
        </CardHeader>
        {rows.length === 0 ? (
          <EmptyState icon={<Inbox />} title="Nobody in this range" className="py-8" />
        ) : (
          <div className="overflow-x-auto">
            <Table aria-label="Daily register">
              <thead>
                <tr>
                  <Th>Date</Th>
                  <Th>Name</Th>
                  <Th>Code</Th>
                  <Th>Department</Th>
                  <Th>First IN</Th>
                  <Th>Last OUT</Th>
                  <Th className="text-right">Hours</Th>
                  <Th>Status</Th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, PREVIEW_ROWS).map((r) => (
                  <tr key={`${r.date}-${r.employee_code}`}>
                    <Td className="tabular-nums">{r.date}</Td>
                    <Td className="font-medium">{r.name}</Td>
                    <Td>{r.employee_code}</Td>
                    <Td>{r.department ?? ""}</Td>
                    <Td className="tabular-nums">{hhmm(r.first_in)}</Td>
                    <Td className="tabular-nums">
                      {hhmm(r.last_out)}
                      {r.missing_out && (
                        <Badge tone="missing" className="ml-1">
                          missing OUT
                        </Badge>
                      )}
                      {r.check && (
                        <Badge tone="error" className="ml-1" title="The last OUT is earlier than the first IN">
                          check
                        </Badge>
                      )}
                    </Td>
                    <Td className="text-right tabular-nums">{hours(r.worked_hours)}</Td>
                    <Td>
                      <StatusBadge status={r.status} today={r.date === today()} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
    </>
  );
}

export function MonthlyView({ people }: { people: PersonTotal[] }) {
  const total = people.reduce((sum, p) => sum + p.total_hours, 0);
  const chart = [...people].sort((a, b) => b.total_hours - a.total_hours).slice(0, 30);
  return (
    <>
      <Summary
        items={[
          ["People", people.length],
          ["Days present", people.reduce((sum, p) => sum + p.days_present, 0)],
          ["Missing exits", people.reduce((sum, p) => sum + p.days_missing_exit, 0)],
          ["Days to check", people.reduce((sum, p) => sum + p.days_check, 0)],
          ["Hours worked", total.toFixed(1)],
        ]}
      />
      {chart.some((p) => p.total_hours > 0) && (
        <Card>
          <CardHeader>
            <CardTitle>Hours worked per person{people.length > chart.length ? " (top 30)" : ""}</CardTitle>
          </CardHeader>
          <CardContent className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chart} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis
                  dataKey="name"
                  interval={0}
                  tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis tick={{ fontSize: 12, fill: "var(--muted-foreground)" }} tickLine={false} axisLine={false} />
                <ChartTooltip
                  cursor={{ fill: "var(--muted)" }}
                  contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", fontSize: 12 }}
                  formatter={(v) => [Number(v).toFixed(2), "hours"]}
                />
                <Bar dataKey="total_hours" fill="var(--primary)" radius={[3, 3, 0, 0]} maxBarSize={28} />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      )}
      <Card>
        <CardHeader>
          <CardTitle>Per person</CardTitle>
        </CardHeader>
        {people.length === 0 ? (
          <EmptyState icon={<Inbox />} title="Nobody in this range" className="py-8" />
        ) : (
          <div className="overflow-x-auto">
            <Table aria-label="Monthly summary">
              <thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Code</Th>
                  <Th>Department</Th>
                  <Th className="text-right">Days present</Th>
                  <Th className="text-right">Missing exits</Th>
                  <Th className="text-right">Missing entries</Th>
                  <Th className="text-right">Days to check</Th>
                  <Th className="text-right">Absent</Th>
                  <Th className="text-right">Total hours</Th>
                  <Th className="text-right">Average</Th>
                </tr>
              </thead>
              <tbody>
                {people.map((p) => (
                  <tr key={p.employee_code}>
                    <Td className="font-medium">{p.name}</Td>
                    <Td>{p.employee_code}</Td>
                    <Td>{p.department ?? ""}</Td>
                    <Td className="text-right tabular-nums">
                      {p.days_present} / {p.days}
                    </Td>
                    <Td className="text-right tabular-nums">{p.days_missing_exit || ""}</Td>
                    <Td className="text-right tabular-nums">{p.days_missing_entry || ""}</Td>
                    <Td className="text-right tabular-nums">{p.days_check || ""}</Td>
                    <Td className="text-right tabular-nums">{p.days_absent || ""}</Td>
                    <Td className="text-right tabular-nums">{hours(p.total_hours)}</Td>
                    <Td className="text-right tabular-nums">{hours(p.avg_hours)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
    </>
  );
}
