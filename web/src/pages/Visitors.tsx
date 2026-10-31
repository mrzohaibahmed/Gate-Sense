import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ScanFace, Search, UserX } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";

import { ApiError, api, resourceUrl } from "@/api/client";
import type { UnknownVisit } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { useVisitorLog } from "@/app/visitorLog";
import { can, useMe } from "@/auth/auth";
import { DirectionBadge } from "@/components/badges";
import { type ColumnMeta, DataTable } from "@/components/DataTable";
import { Badge } from "@/components/ui/card";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Field, Input, NativeSelect, Textarea } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/misc";
import { presetRange } from "@/lib/ranges";
import { hhmm } from "@/lib/utils";

const PAGE_SIZE = 50;
const meta = (m: ColumnMeta) => m;
type VisitStatus = UnknownVisit["status"];

const STATUS: Record<VisitStatus, { label: string; tone: "warn" | "neutral" | "ok" }> = {
  new: { label: "New", tone: "warn" },
  dismissed: { label: "Dismissed", tone: "neutral" },
  identified: { label: "Identified", tone: "ok" },
};

/** "2026-10-30 08:05" from an ISO local timestamp. */
const when = (iso: string) => iso.replace("T", " ").slice(0, 16);

export function VisitStatusBadge({ visit }: { visit: UnknownVisit }) {
  const s = STATUS[visit.status];
  return (
    <Badge tone={s.tone}>
      {s.label}
      {visit.status === "identified" && visit.person_name ? `: ${visit.person_name}` : ""}
    </Badge>
  );
}

function Closest({ visit }: { visit: UnknownVisit }) {
  if (!visit.closest_code) return <span className="text-muted-foreground">nobody enrolled</span>;
  return (
    <span>
      {visit.closest_name ?? visit.closest_code}{" "}
      <span className="text-xs text-muted-foreground tabular-nums">{visit.score.toFixed(2)}</span>
    </span>
  );
}

const COLUMNS: ColumnDef<UnknownVisit>[] = [
  {
    id: "snapshot",
    header: "",
    meta: meta({ label: "Snapshot", className: "w-14" }),
    cell: ({ row }) =>
      row.original.snapshot_url ? (
        <img
          src={resourceUrl(row.original.snapshot_url)}
          alt={`Visitor at ${row.original.camera}, ${when(row.original.first_seen)}`}
          loading="lazy"
          className="size-10 rounded object-cover"
        />
      ) : (
        <div className="flex size-10 items-center justify-center rounded bg-muted text-muted-foreground">
          <UserX className="size-4" aria-hidden />
        </div>
      ),
  },
  {
    accessorKey: "first_seen",
    header: "Seen",
    meta: meta({ label: "Seen", sortKey: "first_seen", className: "whitespace-nowrap" }),
    cell: ({ row }) => {
      const v = row.original;
      const until = hhmm(v.last_seen);
      return (
        <>
          {when(v.first_seen)}
          {until && until !== hhmm(v.first_seen) ? <span className="text-muted-foreground">–{until}</span> : null}
          {v.tracks > 1 ? <span className="ml-1 text-xs text-muted-foreground">({v.tracks} passes)</span> : null}
        </>
      );
    },
  },
  {
    accessorKey: "camera",
    header: "Camera",
    meta: meta({ label: "Camera", sortKey: "camera" }),
    cell: ({ row }) => (
      <span className="inline-flex items-center gap-1">
        {row.original.camera} <DirectionBadge direction={row.original.direction} />
      </span>
    ),
  },
  {
    accessorKey: "score",
    header: "Closest enrolled person",
    meta: meta({ label: "Closest enrolled person", sortKey: "score" }),
    cell: ({ row }) => <Closest visit={row.original} />,
  },
  {
    accessorKey: "status",
    header: "Status",
    meta: meta({ label: "Status", sortKey: "status" }),
    cell: ({ row }) => <VisitStatusBadge visit={row.original} />,
  },
  {
    accessorKey: "note",
    header: "Note",
    meta: meta({ label: "Note", className: "max-w-64 truncate text-muted-foreground" }),
    cell: ({ getValue }) => {
      const note = (getValue() as string | null) ?? "";
      return <span title={note}>{note}</span>;
    },
  },
];

export function VisitorsPage() {
  const { data: me } = useMe();
  const log = useVisitorLog();
  const [params, setParams] = useSearchParams();
  const [weekStart, todayText] = presetRange("week");
  const filters = {
    from: params.get("from") ?? weekStart,
    to: params.get("to") ?? todayText,
    status: params.get("status") ?? "",
    q: params.get("q") ?? "",
  };
  const sort = params.get("sort") ?? "-first_seen";
  const page = Number(params.get("page") ?? 1);
  const [search, setSearch] = useState(filters.q);
  const [openId, setOpenId] = useState<number | null>(null);
  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!("page" in changes)) next.delete("page");
    setParams(next, { replace: true });
  };
  const query = {
    from: filters.from,
    to: filters.to,
    status: (filters.status || null) as VisitStatus | null,
    q: filters.q || null,
  };
  const visits = useQuery({
    queryKey: ["unknowns", "list", query, sort, page],
    queryFn: () => api.get("/api/unknowns", { query: { ...query, sort, page, page_size: PAGE_SIZE } }),
    placeholderData: keepPreviousData,
  });
  const open = visits.data?.items.find((v) => v.id === openId) ?? null;
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
      <PageHeader title="Unknown visitors" />
      <div className="flex flex-col gap-3 px-4 pb-4">
        {log.known && !log.enabled && (
          <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
            The visitor log is off: the cameras don&apos;t record unknown faces.{" "}
            {can(me, "settings:manage")
              ? "Switch it on in Settings > System."
              : "An admin can switch it on in Settings > System."}{" "}
            Visits recorded before stay here until they are deleted after the retention period.
          </p>
        )}
        <DataTable
          columns={COLUMNS}
          data={visits.data?.items}
          total={visits.data?.total}
          page={page}
          pageSize={PAGE_SIZE}
          onPage={(p) => update({ page: String(p) })}
          sort={sort}
          onSort={(s) => update({ sort: s })}
          isLoading={visits.isPending}
          error={visits.error}
          retry={() => void visits.refetch()}
          onRowClick={(v) => setOpenId(v.id)}
          rowLabel={(v) => `Visitor at ${v.camera}, ${when(v.first_seen)}`}
          empty={
            <EmptyState icon={<UserX />} title="No unknown visitors">
              Faces that match nobody enrolled appear here while the visitor log is on.
            </EmptyState>
          }
          toolbar={
            <>
              {dateInput("from", "From")}
              {dateInput("to", "To")}
              <NativeSelect
                aria-label="Status"
                value={filters.status}
                onChange={(e) => update({ status: e.target.value })}
                className="h-8 w-36"
              >
                <option value="">All statuses</option>
                <option value="new">New</option>
                <option value="dismissed">Dismissed</option>
                <option value="identified">Identified</option>
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
                  aria-label="Search the visitors"
                  placeholder="Camera, person, note"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  onBlur={() => search.trim() !== filters.q && update({ q: search.trim() })}
                  className="w-48 pl-7"
                />
              </form>
            </>
          }
        />
      </div>
      <VisitDialog visit={open} onOpenChange={(o) => !o && setOpenId(null)} />
    </>
  );
}

/** One visit: the snapshot, what the camera saw, and the review form (dismiss, identify, note) with its reason. */
export function VisitDialog({
  visit,
  onOpenChange,
}: {
  visit: UnknownVisit | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { data: me } = useMe();
  return (
    <Dialog open={visit !== null} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        {visit && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1">
              <DialogTitle>Unknown visitor</DialogTitle>
              <DialogDescription>
                {visit.camera} ({visit.direction}), {when(visit.first_seen)}
                {hhmm(visit.last_seen) !== hhmm(visit.first_seen) ? ` to ${hhmm(visit.last_seen)}` : ""}
              </DialogDescription>
            </div>
            <div className="grid gap-4 sm:grid-cols-[auto_1fr]">
              {visit.snapshot_url ? (
                <a href={resourceUrl(visit.snapshot_url)} target="_blank" rel="noreferrer">
                  <img
                    src={resourceUrl(visit.snapshot_url)}
                    alt="Snapshot of the visitor"
                    className="max-h-64 rounded-md border object-contain"
                  />
                </a>
              ) : (
                <div className="flex h-40 w-40 items-center justify-center rounded-md bg-muted text-sm text-muted-foreground">
                  No snapshot
                </div>
              )}
              <dl className="grid grid-cols-[auto_1fr] content-start gap-x-4 gap-y-1 text-sm">
                <dt className="text-muted-foreground">Status</dt>
                <dd>
                  <VisitStatusBadge visit={visit} />
                </dd>
                <dt className="text-muted-foreground">Closest</dt>
                <dd>
                  <Closest visit={visit} />
                </dd>
                <dt className="text-muted-foreground">Passes</dt>
                <dd>{visit.tracks}</dd>
                {visit.liveness != null && (
                  <>
                    <dt className="text-muted-foreground">Live score</dt>
                    <dd className="tabular-nums">{visit.liveness.toFixed(2)}</dd>
                  </>
                )}
                {visit.face_px != null && (
                  <>
                    <dt className="text-muted-foreground">Face</dt>
                    <dd>{visit.face_px} px</dd>
                  </>
                )}
                {visit.reviewed_by && (
                  <>
                    <dt className="text-muted-foreground">Reviewed</dt>
                    <dd>
                      {visit.reviewed_by}, {visit.reviewed ? when(visit.reviewed) : ""}
                    </dd>
                  </>
                )}
                {visit.note && (
                  <>
                    <dt className="text-muted-foreground">Note</dt>
                    <dd className="whitespace-pre-wrap">{visit.note}</dd>
                  </>
                )}
              </dl>
            </div>
            {can(me, "people:manage") && (
              <p className="text-xs text-muted-foreground">
                Someone who should be recorded?{" "}
                <Link to="/enrollment" className={buttonVariants({ variant: "link", size: "sm" })}>
                  <ScanFace /> Enroll them
                </Link>{" "}
                with fresh photos, then mark this visit as identified.
              </p>
            )}
            {can(me, "unknowns:manage") && <ReviewForm key={visit.id} visit={visit} />}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function ReviewForm({ visit }: { visit: UnknownVisit }) {
  const queryClient = useQueryClient();
  const { data: me } = useMe();
  const canIdentify = can(me, "people:read"); // the people list (guards can dismiss and note, not identify)
  const [status, setStatus] = useState<VisitStatus>(visit.status);
  const [personId, setPersonId] = useState(visit.person_id ? String(visit.person_id) : "");
  const [note, setNote] = useState(visit.note ?? "");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const people = useQuery({
    queryKey: ["people", "pick"],
    queryFn: () => api.get("/api/people", { query: { page_size: 500, sort: "name" } }),
    enabled: canIdentify && status === "identified",
    staleTime: 60_000,
  });
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (status === "identified" && !personId) {
      setError("Pick who it was.");
      return;
    }
    if (!reason.trim()) {
      setError("A reason is required (it goes to the audit log).");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.patch("/api/unknowns/{visit_id}", {
        path: { visit_id: visit.id },
        body: {
          status,
          person_id: status === "identified" ? Number(personId) : null,
          note,
          reason: reason.trim(),
        },
      });
      setReason("");
      toast.success("Review saved");
      void queryClient.invalidateQueries({ queryKey: ["unknowns"] });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-3 border-t pt-4" aria-label="Review the visit" noValidate>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Status" htmlFor="v-status">
          <NativeSelect id="v-status" value={status} onChange={(e) => setStatus(e.target.value as VisitStatus)}>
            <option value="new">New (not reviewed)</option>
            <option value="dismissed">Dismissed</option>
            {(canIdentify || visit.status === "identified") && <option value="identified">Identified as…</option>}
          </NativeSelect>
        </Field>
        {status === "identified" && (
          <Field label="Who it was" htmlFor="v-person">
            <NativeSelect id="v-person" value={personId} onChange={(e) => setPersonId(e.target.value)}>
              <option value="">Choose…</option>
              {visit.person_id && !people.data?.items.some((p) => p.id === visit.person_id) && (
                <option value={visit.person_id}>{visit.person_name ?? visit.person_code}</option>
              )}
              {people.data?.items.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.employee_code})
                </option>
              ))}
            </NativeSelect>
          </Field>
        )}
      </div>
      <Field label="Note" htmlFor="v-note">
        <Textarea
          id="v-note"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. delivery driver, expected"
        />
      </Field>
      <Field label="Reason (required)" htmlFor="v-reason" error={error ?? undefined}>
        <Textarea
          id="v-reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          aria-invalid={!!error}
          placeholder="e.g. checked with reception"
        />
      </Field>
      <Button type="submit" disabled={busy} className="self-start">
        {busy ? "Saving…" : "Save review"}
      </Button>
    </form>
  );
}
