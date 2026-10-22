import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Download, ScrollText, Search } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router";
import { toast } from "sonner";

import { api } from "@/api/client";
import type { AuditEntry } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { type ColumnMeta, DataTable } from "@/components/DataTable";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/misc";

const PAGE_SIZE = 100;
const meta = (m: ColumnMeta) => m;

function compact(details: Record<string, unknown>): string {
  return Object.entries(details)
    .map(
      ([k, v]) =>
        `${k}: ${Array.isArray(v) && v.length === 2 ? `${String(v[0])} → ${String(v[1])}` : JSON.stringify(v)}`,
    )
    .join("; ");
}

const COLUMNS: ColumnDef<AuditEntry>[] = [
  {
    accessorKey: "timestamp",
    header: "When",
    meta: meta({ label: "When", sortKey: "timestamp", className: "whitespace-nowrap" }),
    cell: ({ getValue }) => String(getValue()).replace("T", " "),
  },
  { accessorKey: "user", header: "User", meta: meta({ label: "User", sortKey: "user" }) },
  {
    accessorKey: "action",
    header: "Action",
    meta: meta({ label: "Action", sortKey: "action" }),
    cell: ({ getValue }) => <span className="font-medium">{String(getValue())}</span>,
  },
  { accessorKey: "target", header: "Target", meta: meta({ label: "Target", sortKey: "target" }) },
  { accessorKey: "reason", header: "Reason", meta: meta({ label: "Reason" }) },
  {
    accessorKey: "details",
    header: "Details",
    meta: meta({ label: "Details", className: "max-w-96 truncate text-xs text-muted-foreground" }),
    cell: ({ getValue }) => {
      const text = compact(getValue() as Record<string, unknown>);
      return <span title={text}>{text}</span>;
    },
  },
];

function csvCell(value: unknown): string {
  const s = typeof value === "string" ? value : JSON.stringify(value);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** Every entry matching the filters (all pages), as CSV with a BOM so Excel reads it as UTF-8. */
export async function auditCsv(query: Record<string, string | null>): Promise<string> {
  const rows: AuditEntry[] = [];
  for (let page = 1; ; page++) {
    const r = await api.get("/api/audit", { query: { ...query, page, page_size: 500 } });
    rows.push(...r.items);
    if (rows.length >= r.total || r.items.length === 0) break;
  }
  const header = ["When", "User", "Action", "Target", "Reason", "Details"];
  const lines = rows.map((e) =>
    [e.timestamp.replace("T", " "), e.user, e.action, e.target, e.reason, e.details].map(csvCell).join(","),
  );
  return "﻿" + [header.join(","), ...lines].join("\r\n") + "\r\n";
}

export function AuditPage() {
  const [params, setParams] = useSearchParams();
  const get = (k: string) => params.get(k) ?? "";
  const filters = { from: get("from"), to: get("to"), user: get("user"), action: get("action"), q: get("q") };
  const sort = params.get("sort") ?? "-timestamp";
  const page = Number(params.get("page") ?? 1);
  const [search, setSearch] = useState(filters.q);
  const [exporting, setExporting] = useState(false);
  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!("page" in changes)) next.delete("page");
    setParams(next, { replace: true });
  };
  const query = Object.fromEntries(Object.entries(filters).map(([k, v]) => [k, v || null])) as Record<
    string,
    string | null
  >;
  const entries = useQuery({
    queryKey: ["audit", query, sort, page],
    queryFn: () => api.get("/api/audit", { query: { ...query, sort, page, page_size: PAGE_SIZE } }),
    placeholderData: keepPreviousData,
  });
  const exportCsv = async () => {
    setExporting(true);
    try {
      const csv = await auditCsv({ ...query, sort });
      const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `gatesense-audit-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(`Export failed: ${String(e)}`);
    } finally {
      setExporting(false);
    }
  };
  const dateInput = (key: "from" | "to", label: string) => (
    <label className="flex items-center gap-1 text-xs text-muted-foreground">
      {label}
      <input
        type="date"
        aria-label={label}
        value={filters[key]}
        onChange={(e) => update({ [key]: e.target.value })}
        className="h-8 rounded-md border border-input bg-card px-2 text-sm text-foreground"
      />
    </label>
  );
  return (
    <>
      <PageHeader title="Audit log">
        <Button variant="outline" onClick={() => void exportCsv()} disabled={exporting}>
          <Download /> {exporting ? "Exporting…" : "Export CSV"}
        </Button>
      </PageHeader>
      <div className="px-4 pb-4">
        <DataTable
          columns={COLUMNS}
          data={entries.data?.items}
          total={entries.data?.total}
          page={page}
          pageSize={PAGE_SIZE}
          onPage={(p) => update({ page: String(p) })}
          sort={sort}
          onSort={(s) => update({ sort: s })}
          isLoading={entries.isPending}
          error={entries.error}
          retry={() => void entries.refetch()}
          empty={<EmptyState icon={<ScrollText />} title="No entries match" />}
          toolbar={
            <>
              {dateInput("from", "From")}
              {dateInput("to", "To")}
              <Input
                aria-label="User"
                placeholder="User"
                defaultValue={filters.user}
                onBlur={(e) => update({ user: e.target.value.trim() })}
                className="w-28"
              />
              <Input
                aria-label="Action"
                placeholder="Action, e.g. edit_event"
                defaultValue={filters.action}
                onBlur={(e) => update({ action: e.target.value.trim() })}
                className="w-44"
              />
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
                  aria-label="Search the audit log"
                  placeholder="Search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  onBlur={() => search.trim() !== filters.q && update({ q: search.trim() })}
                  className="w-40 pl-7"
                />
              </form>
            </>
          }
        />
      </div>
    </>
  );
}
