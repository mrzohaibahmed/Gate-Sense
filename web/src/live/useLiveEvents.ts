import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { AttendanceEvent } from "@/api/types";
import { meKey } from "@/auth/auth";

export type LiveStatus = "connecting" | "open" | "closed";

export type CameraMessage = {
  type: "camera";
  id: number | null;
  name: string;
  status: string;
  fps: number | null;
  error: string | null;
  warning: string | null;
  held_by: string | null;
  last_frame_at: string | null;
};
export type EventMessage = { type: "event" } & AttendanceEvent;
export type ChangedMessage = {
  type: "attendance_changed";
  action: string;
  target: string;
  user: string;
  reason: string;
};
/** One recognition decision of a camera worker (admins only: live:decisions). */
export type Decision = {
  time: string;
  kind: string; // match, unknown, skip, event, cool-down, not saved, closed
  track: number | null;
  name: string | null;
  score: number | null;
  second: number | null;
  margin: number | null;
  face_px: number | null;
  liveness: number | null; // the frame's live score 0-1 (null: not checked); a spoof is a skip "spoof (score)"
  detail: string;
};
export type DecisionsMessage = { type: "decisions"; camera: string; id: number | null; decisions: Decision[] };
export type LiveMessage =
  { type: "hello"; user: string; role: string } | EventMessage | CameraMessage | ChangedMessage | DecisionsMessage;

/** Reconnect delays (ms): 1, 2, 4, 8, then every 15 s. Reset after a connection opens. */
export const BACKOFF = [1000, 2000, 4000, 8000, 15000];
/** The server closes with this when the session ended: don't reconnect, go back to the login. */
export const SESSION_ENDED = 4401;
const KEEP_EVENTS = 50;

/** Queries that show attendance: refetched after a new event or a correction (and after a reconnect). */
export const ATTENDANCE_QUERIES: QueryKey[] = [
  ["overview"],
  ["attendance"],
  ["events"],
  ["person"],
  ["notifications"],
  ["roster"],
  ["shifts"],
  ["reports"],
  ["features"],
];

export function liveUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/api/ws`;
}

export type LiveState = {
  status: LiveStatus;
  /** Newest first, at most 50: the events seen since the page opened. */
  events: EventMessage[];
  /** Latest status per camera name. */
  cameras: Record<string, CameraMessage>;
  /** Latest decisions per camera name, newest last (only sent to admins). */
  decisions: Record<string, Decision[]>;
};

type Options = {
  enabled?: boolean;
  url?: string;
  /** Injectable for tests. */
  createSocket?: (url: string) => WebSocket;
};

/**
 * The live feed (WebSocket /api/ws): keeps itself connected, keeps the recent events and camera statuses, and
 * refreshes the queries a message affects. Use it once, through <LiveProvider>; screens read it with useLive().
 */
export function useLiveEvents({ enabled = true, url, createSocket }: Options = {}): LiveState {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const [events, setEvents] = useState<EventMessage[]>([]);
  const [cameras, setCameras] = useState<Record<string, CameraMessage>>({});
  const [decisions, setDecisions] = useState<Record<string, Decision[]>>({});
  const cameraStatus = useRef<Record<string, string>>({});

  useEffect(() => {
    if (!enabled) return;
    const target = url ?? liveUrl();
    const open = createSocket ?? ((u: string) => new WebSocket(u));
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let stopped = false;
    let everOpened = false;

    const refresh = (keys: QueryKey[]) => {
      for (const queryKey of keys) void queryClient.invalidateQueries({ queryKey });
    };

    const handle = (message: LiveMessage) => {
      switch (message.type) {
        case "event":
          setEvents((list) => [message, ...list.filter((e) => e.id !== message.id)].slice(0, KEEP_EVENTS));
          refresh(ATTENDANCE_QUERIES);
          break;
        case "attendance_changed":
          refresh(ATTENDANCE_QUERIES);
          break;
        case "decisions":
          setDecisions((all) => ({ ...all, [message.camera]: message.decisions }));
          break;
        case "camera": {
          setCameras((all) => ({ ...all, [message.name]: message }));
          const before = cameraStatus.current[message.name];
          cameraStatus.current[message.name] = message.status;
          if (before !== undefined && before !== message.status) refresh([["cameras"], ["overview"]]);
          break;
        }
        default:
          break;
      }
    };

    const connect = () => {
      setStatus("connecting");
      const ws = open(target);
      socket = ws;
      ws.onopen = () => {
        if (everOpened) refresh([...ATTENDANCE_QUERIES, ["cameras"]]); // anything missed while away
        everOpened = true;
        attempt = 0;
        setStatus("open");
      };
      ws.onmessage = (e: MessageEvent) => {
        try {
          handle(JSON.parse(String(e.data)) as LiveMessage);
        } catch {
          /* not JSON: ignore */
        }
      };
      ws.onclose = (e: CloseEvent) => {
        if (socket !== ws) return;
        setStatus("closed");
        if (stopped) return;
        if (e.code === SESSION_ENDED) {
          void queryClient.invalidateQueries({ queryKey: meKey }); // the guards then go to /login
          return;
        }
        const delay = BACKOFF[Math.min(attempt, BACKOFF.length - 1)] ?? 15000;
        attempt += 1;
        timer = setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      stopped = true;
      clearTimeout(timer);
      socket?.close();
    };
  }, [enabled, url, createSocket, queryClient]);

  return { status, events, cameras, decisions };
}
