import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CameraOff, Maximize2, Play, Square, VideoOff } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { api, ApiError, resourceUrl } from "@/api/client";
import type { Camera } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { useFilters } from "@/app/filters";
import { can, useMe } from "@/auth/auth";
import { CameraStateDot, DirectionBadge } from "@/components/badges";
import { PageSkeleton } from "@/components/states";
import { Badge, Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { EmptyState, ErrorState, Tabs, TabsContent, TabsList, TabsTrigger, Td, Th } from "@/components/ui/misc";
import { useLive } from "@/live/LiveProvider";
import type { Decision } from "@/live/useLiveEvents";
import { cn, today } from "@/lib/utils";

import { EventRow } from "./Overview";

const LAYOUTS = [1, 2, 4, 9] as const;
type Layout = (typeof LAYOUTS)[number];
const LAYOUT_KEY = "gatesense.live.layout";
// Browsers open at most 6 connections per server and every MJPEG stream keeps one open; with 4 or 9 cameras
// on screen the tiles show snapshots refreshed twice a second instead, so the page's own requests still get through.
const MJPEG_UP_TO = 2;

function storedLayout(): Layout {
  try {
    const v = Number(window.localStorage.getItem(LAYOUT_KEY));
    return (LAYOUTS as readonly number[]).includes(v) ? (v as Layout) : 4;
  } catch {
    return 4;
  }
}

export function LiveCamerasPage() {
  const { siteId } = useFilters();
  const [layout, setLayoutState] = useState<Layout>(storedLayout);
  const [page, setPage] = useState(0);
  const [full, setFull] = useState<Camera | null>(null);
  const cameras = useQuery({
    queryKey: ["cameras", siteId],
    queryFn: () => api.get("/api/cameras", { query: { site_id: siteId, page_size: 500 } }),
    refetchInterval: 10_000,
  });
  const setLayout = (l: Layout) => {
    setLayoutState(l);
    setPage(0);
    try {
      window.localStorage.setItem(LAYOUT_KEY, String(l));
    } catch {
      /* not remembered */
    }
  };
  if (cameras.isPending) return <PageSkeleton />;
  if (cameras.error) return <ErrorState error={cameras.error} retry={() => void cameras.refetch()} />;
  const all = cameras.data.items;
  const pages = Math.max(1, Math.ceil(all.length / layout));
  const shown = all.slice(page * layout, page * layout + layout);
  return (
    <>
      <PageHeader title="Live cameras">
        <div role="group" aria-label="Cameras per screen" className="flex rounded-md border">
          {LAYOUTS.map((l) => (
            <button
              key={l}
              type="button"
              onClick={() => setLayout(l)}
              aria-pressed={layout === l}
              className={cn(
                "h-8 px-3 text-sm first:rounded-l-md last:rounded-r-md",
                layout === l && "bg-accent font-medium",
              )}
            >
              {l}
            </button>
          ))}
        </div>
        {pages > 1 && (
          <div className="flex items-center gap-1 text-sm">
            <Button variant="outline" size="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
              Previous
            </Button>
            <span className="px-1 text-muted-foreground">
              {page + 1} / {pages}
            </span>
            <Button variant="outline" size="sm" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>
              Next
            </Button>
          </div>
        )}
      </PageHeader>
      <div className="px-4 pb-4">
        {all.length === 0 ? (
          <Card>
            <EmptyState icon={<CameraOff />} title="No cameras">
              Add one under Settings → Cameras.
            </EmptyState>
          </Card>
        ) : (
          <div
            className={cn(
              "grid gap-4",
              layout === 1 && "grid-cols-1",
              layout === 2 && "grid-cols-1 md:grid-cols-2",
              layout === 4 && "grid-cols-1 md:grid-cols-2",
              layout === 9 && "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3",
            )}
          >
            {shown.map((c) => (
              <CameraTile key={c.id} camera={c} stream={layout <= MJPEG_UP_TO} onFullscreen={() => setFull(c)} />
            ))}
          </div>
        )}
      </div>
      <Dialog open={full !== null} onOpenChange={(o) => !o && setFull(null)}>
        <DialogContent className="top-4 max-h-[calc(100vh-32px)] max-w-6xl">
          {full && <FullView camera={all.find((c) => c.id === full.id) ?? full} />}
        </DialogContent>
      </Dialog>
    </>
  );
}

function useCameraState(c: Camera) {
  const { cameras } = useLive();
  const l = cameras[c.name];
  return {
    state: l?.status ?? c.worker.state,
    fps: l?.fps ?? c.worker.fps,
    problem: l ? (l.error ?? l.warning) : (c.worker.last_error ?? c.worker.warning),
  };
}

function StartStop({ camera, state }: { camera: Camera; state: string }) {
  const { data: me } = useMe();
  const queryClient = useQueryClient();
  const running = state !== "stopped";
  const mutation = useMutation({
    mutationFn: async () => {
      if (running) await api.post("/api/cameras/{camera_id}/stop", { path: { camera_id: camera.id } });
      else await api.post("/api/cameras/{camera_id}/start", { path: { camera_id: camera.id } });
    },
    onSuccess: () => {
      toast.success(running ? `${camera.name} stopped` : `${camera.name} starting…`);
      void queryClient.invalidateQueries({ queryKey: ["cameras"] });
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : String(e)),
  });
  if (!can(me, "cameras:control")) return null;
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => mutation.mutate()}
      disabled={mutation.isPending}
      aria-label={running ? `Stop ${camera.name}` : `Start ${camera.name}`}
    >
      {running ? <Square /> : <Play />}
      {running ? "Stop" : "Start"}
    </Button>
  );
}

/** The picture: an MJPEG stream, or a snapshot refreshed twice a second. Keyed by the camera state, so a camera
 * that stops and starts again gets a fresh try after a failed load. */
function Picture({ camera, stream, state }: { camera: Camera; stream: boolean; state: string }) {
  const [tick, setTick] = useState(0);
  const [failed, setFailed] = useState(false);
  const running = state === "running";
  useEffect(() => {
    if (stream || !running) return;
    const t = setInterval(() => setTick((n) => n + 1), 500);
    return () => clearInterval(t);
  }, [stream, running]);
  if (!running || failed) {
    return (
      <div className="flex aspect-video items-center justify-center bg-muted text-sm text-muted-foreground">
        <span className="flex items-center gap-2">
          <VideoOff className="size-4" />
          {state === "starting" ? "Starting…" : running ? "Waiting for a picture…" : "Stopped"}
        </span>
      </div>
    );
  }
  const src = stream
    ? resourceUrl("/api/cameras/{camera_id}/stream", { camera_id: camera.id })
    : resourceUrl("/api/cameras/{camera_id}/snapshot.jpg", { camera_id: camera.id }, { t: tick });
  return (
    <img
      src={src}
      alt={`Live picture of ${camera.name}`}
      className="aspect-video w-full bg-black object-contain"
      onError={() => setFailed(true)}
    />
  );
}

function CameraTile({ camera, stream, onFullscreen }: { camera: Camera; stream: boolean; onFullscreen: () => void }) {
  const { state, fps, problem } = useCameraState(camera);
  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-2 border-b px-2 py-1.5">
        <CameraStateDot state={state} />
        <span className="truncate font-medium">{camera.name}</span>
        <DirectionBadge direction={camera.direction} />
        {state === "running" && fps != null && (
          <span className="text-xs text-muted-foreground">{fps.toFixed(1)} fps</span>
        )}
        <div className="ml-auto flex items-center gap-1">
          <StartStop camera={camera} state={state} />
          <Button variant="ghost" size="icon" onClick={onFullscreen} aria-label={`Open ${camera.name} full screen`}>
            <Maximize2 />
          </Button>
        </div>
      </div>
      <Picture key={state} camera={camera} stream={stream} state={state} />
      {problem && (
        <p className="border-t px-2 py-1 text-xs text-destructive" role="alert">
          {problem}
        </p>
      )}
    </Card>
  );
}

function FullView({ camera }: { camera: Camera }) {
  const { state, fps, problem } = useCameraState(camera);
  const { data: me } = useMe();
  const { decisions } = useLive();
  const events = useQuery({
    queryKey: ["events", "camera", camera.id],
    queryFn: () => api.get("/api/events", { query: { camera: camera.id, from: today(), to: today(), page_size: 30 } }),
  });
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2 pr-8">
        <CameraStateDot state={state} />
        <DialogTitle>{camera.name}</DialogTitle>
        <DirectionBadge direction={camera.direction} />
        {fps != null && state === "running" && <Badge>{fps.toFixed(1)} fps</Badge>}
        {camera.site && <Badge>{camera.site}</Badge>}
        <div className="ml-auto">
          <StartStop camera={camera} state={state} />
        </div>
      </div>
      {problem && (
        <p role="alert" className="text-sm text-destructive">
          {problem}
        </p>
      )}
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="overflow-hidden rounded-lg border lg:col-span-2">
          <Picture key={state} camera={camera} stream state={state} />
        </div>
        <Tabs defaultValue="events" className="flex min-h-0 flex-col rounded-lg border">
          <TabsList className="m-2 self-start">
            <TabsTrigger value="events">Today on this camera</TabsTrigger>
            {can(me, "live:decisions") && <TabsTrigger value="decisions">Decisions</TabsTrigger>}
          </TabsList>
          <TabsContent value="events">
            {events.error ? (
              <ErrorState error={events.error} />
            ) : (events.data?.items.length ?? 0) === 0 ? (
              <EmptyState title={events.isPending ? "Loading…" : "No events yet today"} className="py-8" />
            ) : (
              <ul className="max-h-[60vh] divide-y overflow-y-auto">
                {events.data?.items.map((e) => (
                  <EventRow key={e.id} e={e} />
                ))}
              </ul>
            )}
          </TabsContent>
          {can(me, "live:decisions") && (
            <TabsContent value="decisions">
              <DecisionsTable decisions={decisions[camera.name] ?? []} />
            </TabsContent>
          )}
        </Tabs>
      </div>
    </div>
  );
}

const KIND: Record<string, { label: string; tone: "ok" | "error" | "neutral" | "accent" | "warn" }> = {
  match: { label: "match", tone: "ok" },
  unknown: { label: "unknown", tone: "error" },
  skip: { label: "skip", tone: "neutral" },
  event: { label: "event", tone: "accent" },
  "cool-down": { label: "cool-down", tone: "warn" },
  "not saved": { label: "not saved", tone: "warn" },
  closed: { label: "closed", tone: "neutral" },
};

/** A camera's latest recognition decisions (newest first): for tuning the threshold and margin. */
export function DecisionsTable({ decisions }: { decisions: Decision[] }) {
  if (decisions.length === 0) {
    return (
      <EmptyState title="No decisions yet" className="py-8">
        They appear while faces pass this camera.
      </EmptyState>
    );
  }
  const num = (v: number | null, digits = 3) => (v == null ? "" : v.toFixed(digits));
  return (
    <div className="max-h-[60vh] overflow-auto">
      <table className="w-full text-xs" aria-label="Decisions">
        <thead>
          <tr>
            <Th>Time</Th>
            <Th>Track</Th>
            <Th>Decision</Th>
            <Th>Name</Th>
            <Th className="text-right">Score</Th>
            <Th className="text-right">Second</Th>
            <Th className="text-right">Margin</Th>
            <Th className="text-right">Face px</Th>
            <Th>Detail</Th>
          </tr>
        </thead>
        <tbody>
          {[...decisions].reverse().map((d, i) => {
            const kind = KIND[d.kind] ?? { label: d.kind, tone: "neutral" as const };
            return (
              <tr key={`${d.time}-${d.track}-${i}`}>
                <Td className="whitespace-nowrap">{d.time}</Td>
                <Td>{d.track ?? ""}</Td>
                <Td>
                  <Badge tone={kind.tone}>{kind.label}</Badge>
                </Td>
                <Td>{d.name ?? ""}</Td>
                <Td className="text-right">{num(d.score)}</Td>
                <Td className="text-right">{num(d.second)}</Td>
                <Td className={cn("text-right", d.margin != null && d.margin < 0.05 && "text-warn")}>
                  {num(d.margin)}
                </Td>
                <Td className="text-right">{d.face_px ?? ""}</Td>
                <Td className="text-muted-foreground">{d.detail}</Td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
