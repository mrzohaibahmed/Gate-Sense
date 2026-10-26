import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  DatabaseBackup,
  Download,
  FileText,
  Pencil,
  Plug,
  Plus,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ApiError, api, resourceUrl } from "@/api/client";
import type { Camera, Roi, Setting, Site, TestResult, UserOut } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { shiftModeKey, useShiftsEnabled } from "@/app/shiftMode";
import { can, type Permission, useMe } from "@/auth/auth";
import { CameraStateDot, DirectionBadge } from "@/components/badges";
import { ReasonDialog } from "@/components/ReasonDialog";
import { PageSkeleton, TableSkeleton } from "@/components/states";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Field, Input, NativeSelect } from "@/components/ui/input";
import { ErrorState, Switch, Tabs, TabsContent, TabsList, TabsTrigger, Td, Th } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

import { ShiftsTab } from "./SettingsShifts";

const TABS: { key: string; label: string; permission: Permission }[] = [
  { key: "cameras", label: "Cameras", permission: "cameras:manage" },
  { key: "sites", label: "Sites", permission: "sites:manage" },
  { key: "shifts", label: "Shifts", permission: "shifts:manage" },
  { key: "users", label: "Users and roles", permission: "users:manage" },
  { key: "recognition", label: "Recognition", permission: "settings:manage" },
  { key: "system", label: "System", permission: "settings:manage" },
];

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

export function SettingsPage() {
  const { data: me } = useMe();
  const shifts = useShiftsEnabled();
  const tabs = TABS.filter((t) => can(me, t.permission) && (shifts || t.key !== "shifts"));
  // Controlled: the tab picked stays picked when the list changes (the Shifts tab arrives with the Shifts setting).
  const [tab, setTab] = useState<string | undefined>();
  const current = tabs.some((t) => t.key === tab) ? tab : tabs[0]?.key;
  return (
    <>
      <PageHeader title="Settings" />
      <div className="px-4 pb-4">
        <Tabs value={current} onValueChange={setTab}>
          <TabsList>
            {tabs.map((t) => (
              <TabsTrigger key={t.key} value={t.key}>
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>
          <TabsContent value="cameras" className="mt-4">
            <CamerasTab />
          </TabsContent>
          <TabsContent value="sites" className="mt-4">
            <SitesTab />
          </TabsContent>
          <TabsContent value="shifts" className="mt-4">
            <ShiftsTab />
          </TabsContent>
          <TabsContent value="users" className="mt-4">
            <UsersTab />
          </TabsContent>
          <TabsContent value="recognition" className="mt-4">
            <RecognitionTab />
          </TabsContent>
          <TabsContent value="system" className="mt-4">
            <SystemTab />
          </TabsContent>
        </Tabs>
      </div>
    </>
  );
}

// --- Cameras --------------------------------------------------------------------------------------

type CameraForm = {
  name: string;
  direction: "IN" | "OUT";
  type: "webcam" | "hikvision" | "rtsp" | "file";
  mirror: boolean;
  site_id: string;
  index: string;
  ip: string;
  port: string;
  user: string;
  channel: string;
  stream: "main" | "sub";
  url: string;
  path: string;
  password: string;
  roi: { x: string; y: string; w: string; h: string } | null;
};

function formOf(c: Camera | null): CameraForm {
  return {
    name: c?.name ?? "",
    direction: c?.direction ?? "IN",
    type: c?.type ?? "webcam",
    mirror: c?.mirror ?? false,
    site_id: c?.site_id ? String(c.site_id) : "",
    index: String(c?.index ?? 0),
    ip: c?.ip ?? "",
    port: String(c?.port ?? 554),
    user: c?.user ?? "",
    channel: String(c?.channel ?? 1),
    stream: c?.stream ?? "main",
    url: c?.type === "rtsp" ? String(c.source) : "",
    path: c?.path ?? "",
    password: "",
    roi: c?.roi ? { x: String(c.roi.x), y: String(c.roi.y), w: String(c.roi.w), h: String(c.roi.h) } : null,
  };
}

function bodyOf(f: CameraForm) {
  const roi: Roi | null = f.roi
    ? { x: Number(f.roi.x), y: Number(f.roi.y), w: Number(f.roi.w), h: Number(f.roi.h) }
    : null;
  const common = { direction: f.direction, type: f.type, mirror: f.mirror, roi };
  const site = f.site_id ? { site_id: Number(f.site_id) } : {};
  const byType = {
    webcam: { index: Number(f.index) },
    hikvision: {
      ip: f.ip.trim(),
      port: Number(f.port),
      user: f.user.trim(),
      channel: Number(f.channel),
      stream: f.stream,
      ...(f.password ? { password: f.password } : {}),
    },
    rtsp: { url: f.url.trim() },
    file: { path: f.path.trim() },
  }[f.type];
  return { ...common, ...site, ...byType };
}

function CamerasTab() {
  const queryClient = useQueryClient();
  const cameras = useQuery({
    queryKey: ["cameras", "settings"],
    queryFn: () => api.get("/api/cameras", { query: { page_size: 500 } }),
  });
  const [editing, setEditing] = useState<Camera | "new" | null>(null);
  const [deleting, setDeleting] = useState<Camera | null>(null);
  const [logOf, setLogOf] = useState<Camera | null>(null);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["cameras"] });
  const move = useMutation({
    mutationFn: ({ camera, step }: { camera: Camera; step: -1 | 1 }) =>
      api.post("/api/cameras/{camera_id}/move", {
        path: { camera_id: camera.id },
        body: { step },
      }),
    onSuccess: (list) => {
      queryClient.setQueryData(["cameras", "settings"], (old: typeof cameras.data) =>
        old ? { ...old, items: list } : old,
      );
      refresh();
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : String(e)),
  });
  const setAuto = useMutation({
    mutationFn: ({ camera, enabled }: { camera: Camera; enabled: boolean }) =>
      api.put("/api/cameras/{camera_id}/enabled", {
        path: { camera_id: camera.id },
        body: { enabled, reason: "changed in Settings > Cameras" },
      }),
    onSuccess: (cam) => {
      toast.success(
        cam.enabled
          ? `${cam.name} now starts with GateSense and is restarted whenever it stops`
          : `${cam.name} is no longer started automatically`,
      );
      refresh();
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : String(e)),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Cameras</CardTitle>
        <Button size="sm" onClick={() => setEditing("new")}>
          <Plus /> Add camera
        </Button>
      </CardHeader>
      {cameras.error ? (
        <ErrorState error={cameras.error} retry={() => void cameras.refetch()} />
      ) : cameras.isPending ? (
        <TableSkeleton rows={3} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Direction</Th>
                <Th>Type</Th>
                <Th>Source</Th>
                <Th>Site</Th>
                <Th title="Start with GateSense and restart whenever it stops">Auto</Th>
                <Th>Status</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {cameras.data.items.map((c, i, all) => (
                <tr key={c.id}>
                  <Td className="font-medium">
                    <span className="flex items-center gap-1">
                      <span className="flex flex-col">
                        <button
                          type="button"
                          className="rounded text-muted-foreground hover:bg-muted disabled:opacity-30"
                          disabled={i === 0 || move.isPending}
                          onClick={() => move.mutate({ camera: c, step: -1 })}
                          aria-label={`Move ${c.name} up`}
                        >
                          <ArrowUp className="size-3.5" />
                        </button>
                        <button
                          type="button"
                          className="rounded text-muted-foreground hover:bg-muted disabled:opacity-30"
                          disabled={i === all.length - 1 || move.isPending}
                          onClick={() => move.mutate({ camera: c, step: 1 })}
                          aria-label={`Move ${c.name} down`}
                        >
                          <ArrowDown className="size-3.5" />
                        </button>
                      </span>
                      {c.name}
                    </span>
                  </Td>
                  <Td>
                    <DirectionBadge direction={c.direction} />
                  </Td>
                  <Td>{c.type}</Td>
                  <Td className="max-w-72 truncate text-xs" title={String(c.source)}>
                    {String(c.source)}
                    {c.missing_variables.length > 0 && (
                      <Badge tone="error" className="ml-1">
                        missing {c.missing_variables.join(", ")}
                      </Badge>
                    )}
                    {c.password_set === false && (
                      <Badge tone="warn" className="ml-1">
                        no password
                      </Badge>
                    )}
                  </Td>
                  <Td>{c.site ?? ""}</Td>
                  <Td>
                    <span className="flex items-center gap-1">
                      <Switch
                        checked={c.enabled}
                        disabled={setAuto.isPending}
                        onCheckedChange={(enabled) => setAuto.mutate({ camera: c, enabled })}
                        aria-label={`Start ${c.name} automatically`}
                      />
                      {c.auto_paused && (
                        <Badge tone="warn" title="Stopped by hand: it stays stopped until it is started again">
                          paused
                        </Badge>
                      )}
                    </span>
                  </Td>
                  <Td>
                    <span className="flex items-center gap-1">
                      <CameraStateDot state={c.worker.state} /> {c.worker.state}
                    </span>
                  </Td>
                  <Td>
                    <span className="flex justify-end gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setLogOf(c)} aria-label={`Log of ${c.name}`}>
                        <FileText /> Log
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setEditing(c)} aria-label={`Edit ${c.name}`}>
                        <Pencil /> Edit
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-destructive"
                        disabled={c.worker.state !== "stopped" || all.length === 1}
                        title={
                          c.worker.state !== "stopped"
                            ? "Stop the camera first"
                            : all.length === 1
                              ? "The last camera can't be deleted"
                              : undefined
                        }
                        onClick={() => setDeleting(c)}
                        aria-label={`Delete ${c.name}`}
                      >
                        <Trash2 />
                      </Button>
                    </span>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Dialog open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="max-w-2xl">
          {editing !== null && (
            <CameraEditor
              camera={editing === "new" ? null : editing}
              onDone={() => {
                setEditing(null);
                void queryClient.invalidateQueries({ queryKey: ["cameras"] });
              }}
            />
          )}
        </DialogContent>
      </Dialog>
      <ReasonDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete the ${deleting?.name ?? ""} camera?`}
        description="It is removed from the camera list. Its attendance events stay."
        confirmLabel="Delete camera"
        destructive
        onConfirm={async (reason) => {
          if (!deleting) return;
          await api.delete("/api/cameras/{camera_id}", { path: { camera_id: deleting.id }, query: { reason } });
          toast.success(`${deleting.name} deleted`);
          refresh();
        }}
      />
      <Dialog open={logOf !== null} onOpenChange={(o) => !o && setLogOf(null)}>
        <DialogContent className="max-w-4xl">{logOf && <CameraLogView camera={logOf} />}</DialogContent>
      </Dialog>
    </Card>
  );
}

/** A camera worker's log: the last 500 lines, refreshed every 2 s, and the whole file as a download. */
export function CameraLogView({ camera, refreshMs = 2000 }: { camera: Camera; refreshMs?: number }) {
  const [follow, setFollow] = useState(true);
  const log = useQuery({
    queryKey: ["cameras", "log", camera.id],
    queryFn: () => api.get("/api/cameras/{camera_id}/log", { path: { camera_id: camera.id }, query: { lines: 500 } }),
    refetchInterval: follow ? refreshMs : false,
  });
  const lines = log.data?.lines ?? [];
  return (
    <div className="flex flex-col gap-2">
      <DialogTitle>Log: {camera.name}</DialogTitle>
      <DialogDescription>
        The last {lines.length} line(s)
        {log.data?.updated ? `, written ${new Date(log.data.updated).toLocaleString()}` : ""}. Passwords are masked.
      </DialogDescription>
      <div className="flex items-center gap-2 text-sm">
        <label className="flex items-center gap-2">
          <Switch checked={follow} onCheckedChange={setFollow} aria-label="Auto-refresh" /> Auto-refresh
        </label>
        <Button asChild variant="outline" size="sm" className="ml-auto">
          <a href={resourceUrl("/api/cameras/{camera_id}/log.txt", { camera_id: camera.id })} download>
            <Download /> Download the whole log
          </a>
        </Button>
      </div>
      {log.error ? (
        <ErrorState error={log.error} retry={() => void log.refetch()} />
      ) : (
        <pre
          aria-label="Log lines"
          ref={(el) => {
            if (el && follow) el.scrollTop = el.scrollHeight;
          }}
          className="h-[60vh] overflow-auto rounded-md border bg-muted/40 p-2 font-mono text-xs leading-5 whitespace-pre-wrap"
        >
          {log.isPending
            ? "Loading…"
            : lines.length
              ? lines.join("\n")
              : "The log is empty (the camera hasn't run yet)."}
        </pre>
      )}
    </div>
  );
}

function CameraEditor({ camera, onDone }: { camera: Camera | null; onDone: () => void }) {
  const [f, setF] = useState<CameraForm>(() => formOf(camera));
  const [error, setError] = useState<string | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => api.get("/api/sites", { query: { page_size: 500 } }) });
  const set = <K extends keyof CameraForm>(k: K, v: CameraForm[K]) => setF((x) => ({ ...x, [k]: v }));
  const running = camera !== null && camera.worker.state !== "stopped";

  const save = async () => {
    setBusy("save");
    setError(null);
    try {
      if (camera) {
        await api.patch("/api/cameras/{camera_id}", { path: { camera_id: camera.id }, body: bodyOf(f) });
      } else {
        await api.post("/api/cameras", { body: { name: f.name.trim(), ...bodyOf(f) } });
      }
      toast.success(camera ? `${camera.name} saved` : `${f.name} added`);
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  const runTest = async () => {
    setBusy("test");
    setTest(null);
    try {
      const unchanged = camera && JSON.stringify(formOf(camera)) === JSON.stringify(f);
      setTest(
        await api.post("/api/cameras/test", {
          body: unchanged
            ? { camera_id: camera.id }
            : { ...bodyOf(f), ...(f.password ? { password: f.password } : {}) },
        }),
      );
    } catch (e) {
      setTest({ ok: false, width: null, height: null, fps: null, codec: null, error: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <DialogTitle>{camera ? `Edit ${camera.name}` : "Add a camera"}</DialogTitle>
        <DialogDescription>
          Passwords are never shown again: a Hikvision password goes to .env; for other RTSP URLs write it as ${"{"}
          VARIABLE{"}"} and set the variable in .env.
        </DialogDescription>
      </div>
      {running && (
        <p className="flex items-center gap-2 rounded-md bg-warn/10 px-2 py-1.5 text-sm text-warn">
          <AlertTriangle className="size-4" /> This camera is running: only its site can change. Stop it to edit the
          rest.
        </p>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name" htmlFor="cam-name" hint={camera ? "Names can't change" : "e.g. gate1"}>
          <Input id="cam-name" value={f.name} disabled={!!camera} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="Direction" htmlFor="cam-direction">
          <NativeSelect
            id="cam-direction"
            value={f.direction}
            onChange={(e) => set("direction", e.target.value as "IN" | "OUT")}
          >
            <option value="IN">IN (entrance)</option>
            <option value="OUT">OUT (exit)</option>
          </NativeSelect>
        </Field>
        <Field label="Type" htmlFor="cam-type">
          <NativeSelect
            id="cam-type"
            value={f.type}
            onChange={(e) => set("type", e.target.value as CameraForm["type"])}
          >
            <option value="webcam">Webcam (USB or built-in)</option>
            <option value="hikvision">Hikvision IP camera</option>
            <option value="rtsp">Other IP camera (RTSP URL)</option>
            <option value="file">Video file</option>
          </NativeSelect>
        </Field>
        <Field label="Site" htmlFor="cam-site">
          <NativeSelect id="cam-site" value={f.site_id} onChange={(e) => set("site_id", e.target.value)}>
            <option value="">{camera ? "(keep)" : "Default site"}</option>
            {sites.data?.items.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </NativeSelect>
        </Field>
        {f.type === "webcam" && (
          <Field label="Webcam number" htmlFor="cam-index" hint="0 is the first webcam">
            <Input
              id="cam-index"
              type="number"
              min={0}
              max={9}
              value={f.index}
              onChange={(e) => set("index", e.target.value)}
            />
          </Field>
        )}
        {f.type === "hikvision" && (
          <>
            <Field label="IP address" htmlFor="cam-ip">
              <Input id="cam-ip" value={f.ip} onChange={(e) => set("ip", e.target.value)} placeholder="192.168.1.101" />
            </Field>
            <Field label="User name" htmlFor="cam-user">
              <Input id="cam-user" value={f.user} onChange={(e) => set("user", e.target.value)} />
            </Field>
            <Field
              label="Password"
              htmlFor="cam-pass"
              hint={camera?.password_set ? "Leave empty to keep the saved one" : undefined}
            >
              <Input
                id="cam-pass"
                type="password"
                autoComplete="new-password"
                value={f.password}
                onChange={(e) => set("password", e.target.value)}
              />
            </Field>
            <Field label="RTSP port" htmlFor="cam-port">
              <Input id="cam-port" type="number" value={f.port} onChange={(e) => set("port", e.target.value)} />
            </Field>
            <Field label="Channel" htmlFor="cam-channel">
              <Input
                id="cam-channel"
                type="number"
                min={1}
                value={f.channel}
                onChange={(e) => set("channel", e.target.value)}
              />
            </Field>
            <Field label="Stream" htmlFor="cam-stream">
              <NativeSelect
                id="cam-stream"
                value={f.stream}
                onChange={(e) => set("stream", e.target.value as "main" | "sub")}
              >
                <option value="main">Main (full resolution)</option>
                <option value="sub">Sub (smaller)</option>
              </NativeSelect>
            </Field>
          </>
        )}
        {f.type === "rtsp" && (
          <Field
            label="RTSP URL"
            htmlFor="cam-url"
            className="sm:col-span-2"
            hint="rtsp://user:${GATESENSE_CAM_PASS}@ip:554/..."
          >
            <Input id="cam-url" value={f.url} onChange={(e) => set("url", e.target.value)} />
          </Field>
        )}
        {f.type === "file" && (
          <Field label="Video file" htmlFor="cam-path" className="sm:col-span-2">
            <Input id="cam-path" value={f.path} onChange={(e) => set("path", e.target.value)} />
          </Field>
        )}
        <label className="flex items-center gap-2 text-sm sm:col-span-2">
          <Switch checked={f.mirror} onCheckedChange={(v) => set("mirror", v)} aria-label="Mirror the live view" />
          Mirror the live view (display only)
        </label>
      </div>
      <RoiEditor camera={camera} roi={f.roi} onChange={(roi) => set("roi", roi)} />
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => void save()} disabled={busy !== null}>
          {busy === "save" ? "Saving…" : "Save"}
        </Button>
        <Button variant="outline" onClick={() => void runTest()} disabled={busy !== null || running}>
          <Plug /> {busy === "test" ? "Testing…" : "Test connection"}
        </Button>
        {test && (
          <span role="status" className={cn("text-sm", test.ok ? "text-ok" : "text-destructive")}>
            {test.ok
              ? `Connected: ${test.width}×${test.height}${test.fps ? `, ${test.fps.toFixed(0)} fps` : ""}${test.codec ? `, ${test.codec}` : ""}`
              : test.error}
          </span>
        )}
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

/** Region of interest as fractions of the frame, drawn over the camera's current picture when it runs. */
function RoiEditor({
  camera,
  roi,
  onChange,
}: {
  camera: Camera | null;
  roi: CameraForm["roi"];
  onChange: (roi: CameraForm["roi"]) => void;
}) {
  const box = roi ? { x: Number(roi.x), y: Number(roi.y), w: Number(roi.w), h: Number(roi.h) } : null;
  const valid =
    !box || (box.x >= 0 && box.y >= 0 && box.w > 0 && box.h > 0 && box.x + box.w <= 1 && box.y + box.h <= 1);
  return (
    <fieldset className="flex flex-col gap-2 rounded-md border p-2">
      <legend className="px-1 text-xs text-muted-foreground">Region of interest</legend>
      <label className="flex items-center gap-2 text-sm">
        <Switch
          checked={roi !== null}
          onCheckedChange={(v) => onChange(v ? { x: "0.1", y: "0.1", w: "0.8", h: "0.8" } : null)}
          aria-label="Use a region of interest"
        />
        Only part of the picture (saved now; the camera worker doesn't apply it yet)
      </label>
      {roi && (
        <div className="grid gap-2 sm:grid-cols-[1fr_200px]">
          <div className="grid grid-cols-4 gap-2">
            {(["x", "y", "w", "h"] as const).map((k) => (
              <Field key={k} label={k} htmlFor={`roi-${k}`}>
                <Input
                  id={`roi-${k}`}
                  type="number"
                  step={0.05}
                  min={0}
                  max={1}
                  value={roi[k]}
                  onChange={(e) => onChange({ ...roi, [k]: e.target.value })}
                  aria-invalid={!valid}
                />
              </Field>
            ))}
            {!valid && (
              <p className="col-span-4 text-xs text-destructive">
                The region must lie inside the picture (fractions 0–1).
              </p>
            )}
          </div>
          <div className="relative aspect-video overflow-hidden rounded border bg-muted">
            {camera && camera.worker.state === "running" && (
              <img
                src={resourceUrl("/api/cameras/{camera_id}/snapshot.jpg", { camera_id: camera.id })}
                alt=""
                className="absolute inset-0 size-full object-cover"
              />
            )}
            {box && valid && (
              <div
                className="absolute border-2 border-primary bg-primary/10"
                style={{
                  left: `${box.x * 100}%`,
                  top: `${box.y * 100}%`,
                  width: `${box.w * 100}%`,
                  height: `${box.h * 100}%`,
                }}
              />
            )}
          </div>
        </div>
      )}
    </fieldset>
  );
}

// --- Sites --------------------------------------------------------------------------------------------

function SitesTab() {
  const queryClient = useQueryClient();
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => api.get("/api/sites", { query: { page_size: 500 } }) });
  const [editing, setEditing] = useState<Site | "new" | null>(null);
  const [deleting, setDeleting] = useState<Site | null>(null);
  const [name, setName] = useState("");
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["sites"] });
  const open = (s: Site | "new") => {
    setEditing(s);
    setName(s === "new" ? "" : s.name);
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Sites</CardTitle>
        <Button size="sm" onClick={() => open("new")}>
          <Plus /> Add site
        </Button>
      </CardHeader>
      {sites.error ? (
        <ErrorState error={sites.error} retry={() => void sites.refetch()} />
      ) : sites.isPending ? (
        <TableSkeleton rows={2} />
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>Cameras</Th>
              <Th>People</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {sites.data.items.map((s) => (
              <tr key={s.id}>
                <Td className="font-medium">{s.name}</Td>
                <Td>{s.cameras}</Td>
                <Td>{s.people}</Td>
                <Td className="text-right">
                  <Button variant="ghost" size="sm" onClick={() => open(s)} aria-label={`Edit ${s.name}`}>
                    <Pencil />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={s.cameras + s.people > 0 || sites.data.items.length === 1}
                    onClick={() => setDeleting(s)}
                    aria-label={`Delete ${s.name}`}
                  >
                    <Trash2 />
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <ReasonDialog
        open={editing !== null}
        onOpenChange={(o) => !o && setEditing(null)}
        title={editing === "new" ? "Add a site" : `Edit ${editing?.name ?? ""}`}
        confirmLabel="Save"
        onConfirm={async (reason) => {
          if (editing === "new") await api.post("/api/sites", { body: { name, reason } });
          else if (editing)
            await api.patch("/api/sites/{site_id}", {
              path: { site_id: editing.id },
              body: { name, reason },
            });
          refresh();
        }}
      >
        <Field label="Name" htmlFor="site-name">
          <Input id="site-name" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
      </ReasonDialog>
      <ReasonDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete ${deleting?.name ?? ""}?`}
        confirmLabel="Delete"
        destructive
        onConfirm={async (reason) => {
          if (deleting) await api.delete("/api/sites/{site_id}", { path: { site_id: deleting.id }, query: { reason } });
          refresh();
        }}
      />
    </Card>
  );
}

// --- Users and roles ----------------------------------------------------------------------------------

const ROLES = ["admin", "hr", "viewer", "guard"] as const;
type Role = (typeof ROLES)[number];

function UsersTab() {
  const { data: me } = useMe();
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api.get("/api/users", { query: { page_size: 500 } }) });
  const [dialog, setDialog] = useState<{ kind: "new" | "role" | "active" | "password"; user?: UserOut } | null>(null);
  const [form, setForm] = useState({ username: "", full_name: "", email: "", role: "viewer" as Role, password: "" });
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["users"] });
  const openDialog = (kind: "new" | "role" | "active" | "password", user?: UserOut) => {
    setForm({ username: "", full_name: "", email: "", role: (user?.role as Role) ?? "viewer", password: "" });
    setDialog({ kind, user });
  };
  const u = dialog?.user;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Users and roles</CardTitle>
        <Button size="sm" onClick={() => openDialog("new")}>
          <Plus /> Add user
        </Button>
      </CardHeader>
      {users.error ? (
        <ErrorState error={users.error} retry={() => void users.refetch()} />
      ) : users.isPending ? (
        <TableSkeleton rows={3} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr>
                <Th>User</Th>
                <Th>Name</Th>
                <Th>Role</Th>
                <Th>Status</Th>
                <Th>Last login</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {users.data.items.map((x) => (
                <tr key={x.id}>
                  <Td className="font-medium">{x.username}</Td>
                  <Td>{x.full_name}</Td>
                  <Td>
                    <Badge tone="accent">{x.role}</Badge>
                  </Td>
                  <Td>
                    {!x.active ? (
                      <Badge>inactive</Badge>
                    ) : x.locked_until ? (
                      <Badge tone="error">locked</Badge>
                    ) : x.must_change_password ? (
                      <Badge tone="warn">must change password</Badge>
                    ) : (
                      <Badge tone="ok">active</Badge>
                    )}
                  </Td>
                  <Td className="text-xs">{x.last_login_at?.replace("T", " ") ?? "never"}</Td>
                  <Td className="text-right whitespace-nowrap">
                    <Button variant="ghost" size="sm" onClick={() => openDialog("role", x)}>
                      Role
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => openDialog("password", x)}>
                      Reset password
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={x.id === me?.id}
                      onClick={() => openDialog("active", x)}
                    >
                      {x.active ? "Deactivate" : "Activate"}
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ReasonDialog
        open={dialog !== null}
        onOpenChange={(o) => !o && setDialog(null)}
        title={
          dialog?.kind === "new"
            ? "Add a user"
            : dialog?.kind === "role"
              ? `Role of ${u?.username}`
              : dialog?.kind === "password"
                ? `New temporary password for ${u?.username}`
                : `${u?.active ? "Deactivate" : "Activate"} ${u?.username}?`
        }
        description={
          dialog?.kind === "new" || dialog?.kind === "password"
            ? "They must change this password when they log in."
            : dialog?.kind === "active" && u?.active
              ? "They are logged out at once and can't log in."
              : undefined
        }
        confirmLabel="Save"
        destructive={dialog?.kind === "active" && u?.active}
        onConfirm={async (reason) => {
          if (dialog?.kind === "new") {
            await api.post("/api/users", {
              body: {
                username: form.username,
                full_name: form.full_name,
                email: form.email || null,
                role: form.role,
                password: form.password,
                reason,
              },
            });
          } else if (u && dialog?.kind === "role") {
            await api.patch("/api/users/{user_id}/role", {
              path: { user_id: u.id },
              body: { role: form.role, reason },
            });
          } else if (u && dialog?.kind === "password") {
            await api.post("/api/users/{user_id}/reset-password", {
              path: { user_id: u.id },
              body: { new_password: form.password, reason },
            });
          } else if (u && dialog?.kind === "active") {
            await api.post(u.active ? "/api/users/{user_id}/deactivate" : "/api/users/{user_id}/activate", {
              path: { user_id: u.id },
              body: { reason },
            });
          }
          toast.success("Saved");
          refresh();
        }}
      >
        {dialog?.kind === "new" && (
          <>
            <Field label="User name" htmlFor="u-name">
              <Input
                id="u-name"
                value={form.username}
                onChange={(e) => setForm({ ...form, username: e.target.value })}
              />
            </Field>
            <Field label="Full name" htmlFor="u-full">
              <Input
                id="u-full"
                value={form.full_name}
                onChange={(e) => setForm({ ...form, full_name: e.target.value })}
              />
            </Field>
            <Field label="Email (optional)" htmlFor="u-email">
              <Input
                id="u-email"
                type="email"
                value={form.email}
                onChange={(e) => setForm({ ...form, email: e.target.value })}
              />
            </Field>
          </>
        )}
        {(dialog?.kind === "new" || dialog?.kind === "role") && (
          <Field label="Role" htmlFor="u-role">
            <NativeSelect
              id="u-role"
              value={form.role}
              onChange={(e) => setForm({ ...form, role: e.target.value as Role })}
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </NativeSelect>
          </Field>
        )}
        {(dialog?.kind === "new" || dialog?.kind === "password") && (
          <Field label="Temporary password" htmlFor="u-pass" hint="At least 10 characters">
            <Input
              id="u-pass"
              type="password"
              autoComplete="new-password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
            />
          </Field>
        )}
      </ReasonDialog>
    </Card>
  );
}

// --- Recognition ---------------------------------------------------------------------------------------

const MAIN = ["match_threshold", "match_margin", "cooldown_seconds", "liveness_enabled", "liveness_threshold"];
const LABELS: Record<string, string> = {
  match_threshold: "Match threshold",
  match_margin: "Margin over the second-best",
  cooldown_seconds: "Debounce (cool-down, seconds)",
  snapshot_retention_days: "Keep event snapshots (days)",
  liveness_enabled: "Liveness check (anti-spoofing)",
  liveness_threshold: "Liveness threshold",
  liveness_min_frames: "Live frames needed per person",
};
/** A setting's value for display: on/off for bool settings. */
const shown = (s: Setting, v: number | string) => (s.type === "bool" ? (Number(v) ? "on" : "off") : String(v));

function RecognitionTab() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.get("/api/settings") });
  const [values, setValues] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState(false);
  if (settings.isPending) return <PageSkeleton />;
  if (settings.error) return <ErrorState error={settings.error} retry={() => void settings.refetch()} />;
  const all = settings.data.settings;
  const ordered = [...all.filter((s) => MAIN.includes(s.key)), ...all.filter((s) => !MAIN.includes(s.key))];
  const value = (s: Setting) => values[s.key] ?? String(s.value);
  const changed = all.filter((s) => values[s.key] !== undefined && Number(values[s.key]) !== s.value);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Recognition</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="flex gap-2 rounded-md bg-warn/10 px-2 py-1.5 text-sm text-warn">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          These decide who is recognised. A lower threshold lets in look-alikes; a higher one misses real people. Change
          one thing at a time and watch the live decisions. {settings.data.applies}
        </p>
        <div className="grid gap-4 md:grid-cols-2">
          {ordered.map((s) => (
            <Field
              key={s.key}
              label={`${LABELS[s.key] ?? s.key}${s.changed ? " (changed)" : ""}`}
              htmlFor={`set-${s.key}`}
              hint={
                s.type === "bool"
                  ? `${s.description}. Default ${shown(s, s.default)}.`
                  : `${s.description}. ${s.minimum}–${s.maximum}, default ${s.default}.`
              }
            >
              {s.type === "bool" ? (
                <div className="flex h-9 items-center gap-2">
                  <Switch
                    id={`set-${s.key}`}
                    checked={Number(value(s)) === 1}
                    onCheckedChange={(on) => setValues({ ...values, [s.key]: on ? "1" : "0" })}
                  />
                  <span className="text-sm">{shown(s, value(s))}</span>
                </div>
              ) : (
                <Input
                  id={`set-${s.key}`}
                  type="number"
                  step={s.type === "int" ? 1 : 0.01}
                  min={s.minimum}
                  max={s.maximum}
                  value={value(s)}
                  onChange={(e) => setValues({ ...values, [s.key]: e.target.value })}
                />
              )}
            </Field>
          ))}
        </div>
        {settings.data.running_cameras.length > 0 && (
          <p className="text-xs text-muted-foreground">Running now: {settings.data.running_cameras.join(", ")}</p>
        )}
        <Button className="self-start" disabled={changed.length === 0} onClick={() => setConfirm(true)}>
          Save {changed.length > 0 ? `${changed.length} change(s)` : ""}
        </Button>
      </CardContent>
      <ReasonDialog
        open={confirm}
        onOpenChange={setConfirm}
        title="Change recognition settings?"
        description={changed
          .map((s) => `${LABELS[s.key] ?? s.key}: ${shown(s, s.value)} → ${shown(s, values[s.key] ?? s.value)}`)
          .join("; ")}
        confirmLabel="Save"
        onConfirm={async (reason) => {
          await api.patch("/api/settings", {
            body: { values: Object.fromEntries(changed.map((s) => [s.key, Number(values[s.key])])), reason },
          });
          setValues({});
          toast.success("Settings saved. Restart running cameras to apply them.");
          void queryClient.invalidateQueries({ queryKey: ["settings"] });
        }}
      />
    </Card>
  );
}

// --- System ---------------------------------------------------------------------------------------------

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

function SystemTab() {
  const queryClient = useQueryClient();
  const info = useQuery({ queryKey: ["system"], queryFn: () => api.get("/api/system") });
  const backup = useMutation({
    mutationFn: () => api.post("/api/system/backup", { body: { reason: "backup from the dashboard" } }),
    onSuccess: (b) => {
      if (b.copy_error) toast.error(`Backup saved in data/backups, but not copied: ${b.copy_error}`);
      else toast.success(`Backup saved: ${b.file}${b.copied_to ? ` (and copied to ${b.copied_to})` : ""}`);
      void queryClient.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  if (info.isPending) return <PageSkeleton />;
  if (info.error) return <ErrorState error={info.error} retry={() => void info.refetch()} />;
  const s = info.data;
  const hours = Math.floor(s.uptime_seconds / 3600);
  const rows: [string, React.ReactNode][] = [
    ["Version", s.version],
    ["Running for", `${hours} h ${Math.floor((s.uptime_seconds % 3600) / 60)} min`],
    ["Python", s.python],
    ["Database", `${bytes(s.database_bytes)} · schema ${s.database_schema}`],
    ["Events", s.events],
    ["People", `${s.people} (${s.enrolled} enrolled)`],
    ["Face data", bytes(s.gallery_bytes)],
    ["Cameras running", s.cameras_running],
    ["Snapshots", `${bytes(s.maintenance.snapshots_bytes)} · kept ${s.maintenance.snapshot_retention_days} days`],
    [
      "Nightly backup",
      s.maintenance.last_run ? (
        <span className={s.maintenance.last_ok ? undefined : "text-destructive"}>
          {s.maintenance.last_ok ? "last run " : "failed "}
          {s.maintenance.last_run.replace("T", " ").slice(0, 16)}
          {s.maintenance.last_error ? ` (${s.maintenance.last_error})` : ""}
        </span>
      ) : (
        `not run yet (daily after ${String(s.maintenance.backup_hour).padStart(2, "0")}:00)`
      ),
    ],
    ["Second backup folder", s.maintenance.backup_copy_dir ?? "none (set GATESENSE_BACKUP_COPY_DIR)"],
  ];
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>System</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            {rows.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted-foreground">{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
      <ShiftsSwitch />
      <Card>
        <CardHeader>
          <CardTitle>Backups</CardTitle>
          <Button size="sm" onClick={() => backup.mutate()} disabled={backup.isPending}>
            <DatabaseBackup /> {backup.isPending ? "Backing up…" : "Back up now"}
          </Button>
        </CardHeader>
        {s.backups.length === 0 ? (
          <p className="p-4 text-sm text-muted-foreground">
            No backups yet. They go to data/backups/: the database, face data, enrollment photos and camera and
            threshold settings (never .env). The newest {s.maintenance.backup_keep} nightly sets are kept; "Back up now"
            sets until you delete them. Restore with scripts\restore.py.
          </p>
        ) : (
          <ul className="max-h-72 divide-y overflow-auto text-sm">
            {s.backups.map((b) => (
              <li key={b.file} className="flex items-center gap-2 px-4 py-2">
                <span className="truncate">{b.file}</span>
                <span className="ml-auto text-xs whitespace-nowrap text-muted-foreground">
                  {bytes(b.size_bytes)} · {b.created.replace("T", " ").slice(0, 16)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

/**
 * Shifts on or off (admin, with a reason; audited). Off: attendance by calendar day as before the shift module, and
 * the Roster page and Settings > Shifts are hidden. Nothing is deleted: switching back shows the shift results again.
 */
function ShiftsSwitch() {
  const queryClient = useQueryClient();
  const mode = useQuery({ queryKey: shiftModeKey, queryFn: () => api.get("/api/settings/shifts") });
  const [asking, setAsking] = useState<boolean | null>(null);
  if (!mode.data) return null;
  const on = mode.data.enabled;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Shifts</CardTitle>
        <label className="flex items-center gap-2 text-sm">
          <Switch checked={on} onCheckedChange={(v) => setAsking(v)} aria-label="Shifts" />
          {on ? "On" : "Off"}
        </label>
      </CardHeader>
      <CardContent className="text-sm text-muted-foreground">
        {on
          ? "On: attendance by roster day (shift windows, late, early leave, overtime, absent, off and unscheduled days); the Roster page and Settings > Shifts are in use."
          : "Off: attendance by calendar day, as before shifts (first IN and last OUT of the day, worked time, missing IN / OUT); the Roster page and Settings > Shifts are hidden. The shifts and the roster are kept."}
        {mode.data.updated_by && (
          <span className="block pt-1 text-xs">
            Last switched by {mode.data.updated_by}, {mode.data.updated?.replace("T", " ").slice(0, 16)}
          </span>
        )}
      </CardContent>
      <ReasonDialog
        open={asking !== null}
        onOpenChange={(o) => !o && setAsking(null)}
        title={asking ? "Switch shifts on?" : "Switch shifts off?"}
        description={
          asking
            ? "Attendance is worked out by roster day again, from the same camera passes."
            : "Attendance is worked out by calendar day (first IN, last OUT). The shifts and the roster are kept; switch back any time."
        }
        confirmLabel={asking ? "Switch on" : "Switch off"}
        onConfirm={async (reason) => {
          await api.put("/api/settings/shifts", { body: { enabled: !!asking, reason } });
          for (const k of [
            ["features"],
            ["attendance"],
            ["overview"],
            ["reports"],
            ["events"],
            ["person"],
            ["roster"],
          ]) {
            void queryClient.invalidateQueries({ queryKey: k });
          }
        }}
      />
    </Card>
  );
}
