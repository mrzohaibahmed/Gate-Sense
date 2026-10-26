import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  ChevronLeft,
  ChevronRight,
  ImageOff,
  ImagePlus,
  Pencil,
  ScanFace,
  Search,
  Trash2,
  Upload,
  UserPlus,
  Users,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";

import { api, resourceUrl } from "@/api/client";
import type { AttendanceRow, Person, Status } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { useFilters } from "@/app/filters";
import { useShiftsEnabled } from "@/app/shiftMode";
import { can, useMe } from "@/auth/auth";
import { DirectionBadge, StatusBadge, statusLabel } from "@/components/badges";
import { type ColumnMeta, DataTable } from "@/components/DataTable";
import { ReasonDialog } from "@/components/ReasonDialog";
import { PageSkeleton } from "@/components/states";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { EmptyState, ErrorState, Td, Th } from "@/components/ui/misc";
import { shiftLabel, timeOn } from "@/lib/days";
import { cn, duration, hhmm, today } from "@/lib/utils";

import { ImportPeopleDialog } from "./ImportPeople";
import { DeleteFaceDataDialog, DeletePhotoDialog, EditPersonDialog } from "./PersonDialogs";

const PAGE_SIZE = 100;

export function Avatar({ url, name, className }: { url: string | null | undefined; name: string; className?: string }) {
  return url ? (
    <img src={resourceUrl(url)} alt="" className={cn("size-8 rounded-full object-cover", className)} loading="lazy" />
  ) : (
    <span
      aria-hidden
      className={cn(
        "flex size-8 items-center justify-center rounded-full bg-muted text-xs text-muted-foreground",
        className,
      )}
    >
      {name.slice(0, 1).toUpperCase()}
    </span>
  );
}

const meta = (m: ColumnMeta) => m;
const COLUMNS: ColumnDef<Person>[] = [
  {
    id: "photo",
    header: "",
    enableHiding: false,
    cell: ({ row }) => <Avatar url={row.original.photo_url} name={row.original.name} />,
    meta: meta({ className: "w-10" }),
  },
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
    accessorKey: "shift",
    header: "Shift",
    meta: meta({ label: "Shift", sortKey: "shift" }),
    cell: ({ getValue }) => (getValue() as string | null) ?? "",
  },
  { accessorKey: "site", header: "Site", meta: meta({ label: "Site", sortKey: "site" }) },
  {
    accessorKey: "active",
    header: "Status",
    meta: meta({ label: "Status", sortKey: "active" }),
    cell: ({ row }) => (row.original.active ? <Badge tone="ok">Active</Badge> : <Badge tone="neutral">Inactive</Badge>),
  },
  {
    accessorKey: "embeddings",
    header: "Enrolled photos",
    meta: meta({ label: "Enrolled photos", sortKey: "embeddings" }),
    cell: ({ row }) =>
      row.original.embeddings ? (
        `${row.original.embeddings} of ${row.original.photos}`
      ) : (
        <Badge tone="warn">not enrolled</Badge>
      ),
  },
];

export function PeoplePage() {
  const { data: me } = useMe();
  const { siteId } = useFilters();
  const navigate = useNavigate();
  const [importing, setImporting] = useState(false);
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const active = params.get("active") ?? "true";
  const sort = params.get("sort") ?? "name";
  const page = Number(params.get("page") ?? 1);
  const [search, setSearch] = useState(q);
  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    if (!("page" in changes)) next.delete("page");
    setParams(next, { replace: true });
  };
  const people = useQuery({
    queryKey: ["people", "table", { q, active, sort, page, siteId }],
    queryFn: () =>
      api.get("/api/people", {
        query: {
          q: q || null,
          active: active === "all" ? null : active === "true",
          sort,
          page,
          page_size: PAGE_SIZE,
          site_id: siteId,
        },
      }),
    placeholderData: keepPreviousData,
  });
  return (
    <>
      <PageHeader title="People">
        {can(me, "people:manage") && (
          <>
            <Button variant="outline" onClick={() => setImporting(true)}>
              <Upload /> Import from file
            </Button>
            <Button asChild>
              <Link to="/enrollment">
                <UserPlus /> Enroll a person
              </Link>
            </Button>
          </>
        )}
      </PageHeader>
      <ImportPeopleDialog open={importing} onOpenChange={setImporting} />
      <div className="px-4 pb-4">
        <DataTable
          columns={COLUMNS}
          data={people.data?.items}
          total={people.data?.total}
          page={page}
          pageSize={PAGE_SIZE}
          onPage={(p) => update({ page: String(p) })}
          sort={sort}
          onSort={(s) => update({ sort: s })}
          isLoading={people.isPending}
          error={people.error}
          retry={() => void people.refetch()}
          onRowClick={(p) => navigate(`/people/${p.id}`)}
          rowLabel={(p) => p.name}
          initialHidden={["shift", "site"]}
          empty={
            <EmptyState icon={<Users />} title="Nobody found">
              {q ? "Try another name or code." : "Enroll the first person on the Enrollment page."}
            </EmptyState>
          }
          toolbar={
            <>
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
                  aria-label="Search people"
                  placeholder="Name, code, department"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  onBlur={() => search.trim() !== q && update({ q: search.trim() })}
                  className="w-56 pl-7"
                />
              </form>
              <NativeSelect aria-label="Active" value={active} onChange={(e) => update({ active: e.target.value })}>
                <option value="true">Active</option>
                <option value="false">Inactive</option>
                <option value="all">Everyone</option>
              </NativeSelect>
            </>
          }
        />
      </div>
    </>
  );
}

// --- Profile ------------------------------------------------------------------------------------

function monthBounds(month: string): { from: string; to: string; days: string[] } {
  const [y, m] = month.split("-").map(Number) as [number, number];
  const last = new Date(y, m, 0).getDate();
  const days = Array.from({ length: last }, (_, i) => `${month}-${String(i + 1).padStart(2, "0")}`);
  const end = days[days.length - 1]!;
  return { from: days[0]!, to: end < today() ? end : today(), days };
}

function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map(Number) as [number, number];
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

const CELL: Record<Status, string> = {
  "on time": "bg-ok/15 text-ok",
  late: "bg-warn/15 text-warn",
  "early leave": "bg-warn/15 text-warn",
  "missing IN": "bg-status-missing/15 text-status-missing",
  "missing OUT": "bg-status-missing/15 text-status-missing",
  check: "bg-destructive/15 text-destructive",
  absent: "bg-status-absent/10 text-status-absent",
  "not in yet": "bg-muted text-muted-foreground",
  "off day": "bg-muted/60 text-muted-foreground",
  unscheduled: "bg-primary/10 text-primary",
  complete: "bg-status-left/15 text-status-left",
};

/** A month of days coloured by status; hover shows the times. */
export function AttendanceCalendar({ rows, month }: { rows: AttendanceRow[]; month: string }) {
  const { days } = monthBounds(month);
  const byDay = new Map(rows.map((r) => [r.date, r]));
  const [y, m] = month.split("-").map(Number) as [number, number];
  const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7; // Monday first
  return (
    <div>
      <div className="grid grid-cols-7 gap-1 text-center text-xs text-muted-foreground">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
          <span key={d}>{d}</span>
        ))}
      </div>
      <div className="mt-1 grid grid-cols-7 gap-1" role="grid" aria-label={`Attendance in ${month}`}>
        {Array.from({ length: lead }, (_, i) => (
          <span key={`lead${i}`} />
        ))}
        {days.map((d) => {
          const r = byDay.get(d);
          const future = d > today();
          return (
            <span
              key={d}
              role="gridcell"
              title={
                r
                  ? `${d}: ${statusLabel(r.status)} (${shiftLabel(r)})${r.first_in ? `, IN ${timeOn(r.first_in, d)}` : ""}${r.last_out ? `, OUT ${timeOn(r.last_out, d)}` : ""}`
                  : d
              }
              className={cn(
                "flex h-9 flex-col items-center justify-center rounded text-xs",
                future ? "text-muted-foreground/40" : r ? CELL[r.status] : "bg-muted/40",
              )}
            >
              <span className="font-medium">{Number(d.slice(8))}</span>
              {r?.first_in && <span className="text-[10px] leading-3">{hhmm(r.first_in)}</span>}
            </span>
          );
        })}
      </div>
    </div>
  );
}

export function PersonProfilePage() {
  const { id } = useParams();
  const personId = Number(id);
  const shifts = useShiftsEnabled();
  const { data: me } = useMe();
  const queryClient = useQueryClient();
  const [month, setMonth] = useState(today().slice(0, 7));
  const [dialog, setDialog] = useState<null | "active" | "reenroll" | "edit" | "faceData">(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const person = useQuery({
    queryKey: ["person", personId],
    queryFn: () => api.get("/api/people/{person_id}", { path: { person_id: personId } }),
    enabled: Number.isFinite(personId),
  });
  const bounds = useMemo(() => monthBounds(month), [month]);
  const calendar = useQuery({
    queryKey: ["attendance", "calendar", personId, month],
    queryFn: () =>
      api.get("/api/attendance", { query: { person: personId, from: bounds.from, to: bounds.to, page_size: 500 } }),
    enabled: Number.isFinite(personId) && bounds.from <= today(),
  });
  const refresh = () => {
    for (const key of [["person"], ["people"]]) void queryClient.invalidateQueries({ queryKey: key });
  };
  const toggleActive = useMutation({
    mutationFn: (reason: string) =>
      api.post(person.data?.active ? "/api/people/{person_id}/deactivate" : "/api/people/{person_id}/activate", {
        path: { person_id: personId },
        body: { reason },
      }),
    onSuccess: (p) => {
      toast.success(p.active ? `${p.name} is active again` : `${p.name} deactivated`);
      refresh();
    },
  });
  const reenroll = useMutation({
    mutationFn: (reason: string) =>
      api.post("/api/people/{person_id}/reenroll", { path: { person_id: personId }, body: { reason } }),
    onSuccess: (r) => {
      toast.success(`Re-enrolled: ${r.accepted.length} photo(s) accepted, ${r.rejected.length} refused`);
      refresh();
    },
  });

  if (person.isPending) return <PageSkeleton />;
  if (person.error) return <ErrorState error={person.error} retry={() => void person.refetch()} />;
  const p = person.data;
  const manage = can(me, "people:manage");
  return (
    <>
      <PageHeader title={p.name}>
        {manage && (
          <>
            <Button variant="outline" onClick={() => setDialog("edit")}>
              <Pencil /> Edit
            </Button>
            <Button asChild variant="outline">
              <Link to={`/enrollment?person=${p.id}`}>
                <ScanFace /> Take new photos
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link to={`/enrollment?person=${p.id}&tab=upload`}>
                <ImagePlus /> Add photos from files
              </Link>
            </Button>
            <Button variant="outline" disabled={!p.photos} onClick={() => setDialog("reenroll")}>
              Re-enroll
            </Button>
            <Button variant={p.active ? "destructive" : "default"} onClick={() => setDialog("active")}>
              {p.active ? "Deactivate" : "Activate"}
            </Button>
          </>
        )}
      </PageHeader>
      <div className="grid gap-4 px-4 pb-4 lg:grid-cols-3">
        <Card>
          <CardContent className="flex items-center gap-4">
            <Avatar url={p.photo_url} name={p.name} className="size-16 text-lg" />
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              <dt className="text-muted-foreground">Code</dt>
              <dd>{p.employee_code}</dd>
              <dt className="text-muted-foreground">Department</dt>
              <dd>{p.department ?? "—"}</dd>
              <dt className="text-muted-foreground">Shift</dt>
              <dd>{p.shift ?? "—"}</dd>
              <dt className="text-muted-foreground">Site</dt>
              <dd>{p.site ?? "—"}</dd>
              <dt className="text-muted-foreground">Status</dt>
              <dd>{p.active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>}</dd>
              <dt className="text-muted-foreground">Enrolled</dt>
              <dd>
                {p.embeddings} of {p.photos} photo(s)
              </dd>
            </dl>
          </CardContent>
        </Card>
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Photos</CardTitle>
            {manage && (p.photos > 0 || p.embeddings > 0) && (
              <Button variant="ghost" size="sm" className="text-destructive" onClick={() => setDialog("faceData")}>
                <Trash2 /> Delete face data
              </Button>
            )}
          </CardHeader>
          <CardContent>
            {p.photo_list.length === 0 ? (
              <EmptyState icon={<ImageOff />} title="No photos yet" className="py-4">
                {manage ? "Use “Take new photos” to enroll this person." : "Not enrolled yet."}
              </EmptyState>
            ) : (
              <ul className="flex flex-wrap gap-2">
                {p.photo_list.map((ph) => (
                  <li key={ph.file} className="flex w-24 flex-col gap-1 text-xs">
                    <img
                      src={resourceUrl(ph.url)}
                      alt={`Photo ${ph.file}`}
                      className="aspect-square w-24 rounded-md border object-cover"
                    />
                    <span className="flex items-center gap-1">
                      {ph.accepted === false ? (
                        <Badge tone="error" title={ph.message ?? ""}>
                          refused
                        </Badge>
                      ) : ph.accepted ? (
                        <Badge tone="ok">{ph.score != null ? `${Math.round(ph.score)}` : "ok"}</Badge>
                      ) : (
                        <Badge>not checked</Badge>
                      )}
                      {ph.step && <span className="text-muted-foreground">{ph.step}</span>}
                      {manage && (
                        <button
                          type="button"
                          className="ml-auto rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-destructive"
                          onClick={() => setDeleting(ph.file)}
                          aria-label={`Delete photo ${ph.file}`}
                        >
                          <Trash2 className="size-3.5" />
                        </button>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setMonth(shiftMonth(month, -1))}
              aria-label="Previous month"
            >
              <ChevronLeft />
            </Button>
            <CardTitle>{month}</CardTitle>
            <Button
              variant="ghost"
              size="icon"
              disabled={month >= today().slice(0, 7)}
              onClick={() => setMonth(shiftMonth(month, 1))}
              aria-label="Next month"
            >
              <ChevronRight />
            </Button>
          </CardHeader>
          <CardContent>
            {calendar.error ? (
              <ErrorState error={calendar.error} />
            ) : (
              <AttendanceCalendar rows={calendar.data?.items ?? []} month={month} />
            )}
          </CardContent>
        </Card>
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Last 14 days</CardTitle>
            <Link
              to={`/attendance?person=${p.id}&from=${bounds.from}`}
              className="text-xs text-primary hover:underline"
            >
              All attendance
            </Link>
          </CardHeader>
          <div className="max-h-96 overflow-auto">
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <Th>Date</Th>
                  {shifts && <Th>Shift</Th>}
                  <Th>Status</Th>
                  <Th>First IN</Th>
                  <Th>Last OUT</Th>
                  <Th>Worked</Th>
                  <Th>Events</Th>
                </tr>
              </thead>
              <tbody>
                {p.recent_attendance.map((r) => {
                  const evs = p.recent_events.filter((e) => e.timestamp.startsWith(r.date));
                  return (
                    <tr key={r.date}>
                      <Td>{r.date}</Td>
                      {shifts && <Td className="whitespace-nowrap">{shiftLabel(r)}</Td>}
                      <Td>
                        <StatusBadge status={r.status} inside={r.open && r.inside} />
                      </Td>
                      <Td>{timeOn(r.first_in, r.date)}</Td>
                      <Td>{timeOn(r.last_out, r.date)}</Td>
                      <Td>{duration(r.worked_minutes)}</Td>
                      <Td>
                        <span className="flex flex-wrap gap-1">
                          {evs
                            .slice()
                            .reverse()
                            .map((e) => (
                              <span key={e.id} className={cn("flex items-center gap-1", e.voided && "opacity-50")}>
                                <DirectionBadge direction={e.direction} />
                                {hhmm(e.timestamp)}
                              </span>
                            ))}
                        </span>
                      </Td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
      <ReasonDialog
        open={dialog === "active"}
        onOpenChange={(o) => !o && setDialog(null)}
        title={p.active ? `Deactivate ${p.name}?` : `Activate ${p.name}?`}
        description={
          p.active
            ? "The cameras stop recording this person. Their attendance history stays."
            : "The cameras record this person again."
        }
        confirmLabel={p.active ? "Deactivate" : "Activate"}
        destructive={p.active}
        onConfirm={(reason) => toggleActive.mutateAsync(reason)}
      />
      <ReasonDialog
        open={dialog === "reenroll"}
        onOpenChange={(o) => !o && setDialog(null)}
        title={`Re-enroll ${p.name}?`}
        description="Every photo is checked again and the face data rebuilt. Running cameras pick it up within seconds."
        confirmLabel="Re-enroll"
        onConfirm={(reason) => reenroll.mutateAsync(reason)}
      />
      <EditPersonDialog
        person={p}
        open={dialog === "edit"}
        onOpenChange={(o) => setDialog(o ? "edit" : null)}
        onSaved={() => {
          toast.success("Saved");
          refresh();
        }}
      />
      <DeletePhotoDialog
        person={p}
        file={deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        onDone={() => {
          toast.success("Photo deleted; face data rebuilt");
          refresh();
        }}
      />
      <DeleteFaceDataDialog
        person={p}
        open={dialog === "faceData"}
        onOpenChange={(o) => setDialog(o ? "faceData" : null)}
        onDone={(deactivated) => {
          toast.success(deactivated ? "Face data deleted and person deactivated" : "Face data deleted");
          refresh();
        }}
      />
    </>
  );
}
