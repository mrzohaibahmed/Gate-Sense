import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { testQueryClient, withClient } from "@/test/render";

import { BACKOFF, SESSION_ENDED, useLiveEvents } from "./useLiveEvents";

class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  open() {
    this.onopen?.();
  }
  send(message: object) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
  drop(code = 1006) {
    this.onclose?.({ code });
  }
  close() {
    this.closed = true;
  }
}

const create = (url: string) => new FakeSocket(url) as unknown as WebSocket;
const latest = () => FakeSocket.all[FakeSocket.all.length - 1]!;

const event = {
  type: "event",
  id: 7,
  timestamp: "2026-10-22T08:05:00",
  original_timestamp: null,
  person_id: 1,
  employee_code: "1001",
  name: "Ayesha",
  camera_id: 1,
  camera: "entrance",
  direction: "IN",
  site_id: 1,
  score: 0.71,
  manual: false,
  voided: false,
  snapshot_url: null,
};

beforeEach(() => {
  FakeSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

function setup() {
  const client = testQueryClient();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const hook = renderHook(() => useLiveEvents({ url: "ws://test/api/ws", createSocket: create }), {
    wrapper: withClient(client),
  });
  return { client, invalidate, hook };
}

const invalidated = (spy: { mock: { calls: unknown[][] } }) =>
  spy.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey));

describe("useLiveEvents", () => {
  it("connects, keeps new events and refreshes the attendance queries", () => {
    const { invalidate, hook } = setup();
    expect(hook.result.current.status).toBe("connecting");
    act(() => latest().open());
    expect(hook.result.current.status).toBe("open");

    act(() => latest().send(event));
    expect(hook.result.current.events.map((e) => e.name)).toEqual(["Ayesha"]);
    expect(invalidated(invalidate)).toEqual(
      expect.arrayContaining(['["overview"]', '["attendance"]', '["events"]', '["person"]']),
    );

    invalidate.mockClear();
    act(() =>
      latest().send({ type: "attendance_changed", action: "edit_event", target: "event:7", user: "hr", reason: "x" }),
    );
    expect(invalidated(invalidate)).toContain('["attendance"]');
  });

  it("keeps camera statuses and refetches cameras only when one changes", () => {
    const { invalidate, hook } = setup();
    act(() => latest().open());
    const cam = { type: "camera", id: 1, name: "entrance", status: "running", fps: 7.9, error: null, warning: null };
    act(() => latest().send(cam));
    act(() => latest().send(cam));
    expect(hook.result.current.cameras.entrance?.fps).toBe(7.9);
    expect(invalidated(invalidate)).not.toContain('["cameras"]');
    act(() => latest().send({ ...cam, status: "stopped" }));
    expect(invalidated(invalidate)).toContain('["cameras"]');
  });

  it("reconnects with growing delays and catches up after reconnecting", () => {
    const { invalidate, hook } = setup();
    act(() => latest().open());
    act(() => latest().drop());
    expect(hook.result.current.status).toBe("closed");
    expect(FakeSocket.all).toHaveLength(1);
    act(() => vi.advanceTimersByTime(BACKOFF[0]!));
    expect(FakeSocket.all).toHaveLength(2);
    act(() => latest().drop()); // the retry fails too: the next wait is longer
    act(() => vi.advanceTimersByTime(BACKOFF[0]!));
    expect(FakeSocket.all).toHaveLength(2);
    act(() => vi.advanceTimersByTime(BACKOFF[1]! - BACKOFF[0]!));
    expect(FakeSocket.all).toHaveLength(3);
    invalidate.mockClear();
    act(() => latest().open());
    expect(hook.result.current.status).toBe("open");
    expect(invalidated(invalidate)).toEqual(expect.arrayContaining(['["attendance"]', '["cameras"]']));
  });

  it("stops when the session ended and asks for the user again", () => {
    const { invalidate } = setup();
    act(() => latest().open());
    act(() => latest().drop(SESSION_ENDED));
    act(() => vi.advanceTimersByTime(60_000));
    expect(FakeSocket.all).toHaveLength(1);
    expect(invalidated(invalidate)).toContain('["me"]');
  });

  it("closes the socket when unmounted", () => {
    const { hook } = setup();
    act(() => latest().open());
    hook.unmount();
    expect(latest().closed).toBe(true);
    act(() => vi.advanceTimersByTime(60_000));
    expect(FakeSocket.all).toHaveLength(1);
  });
});

describe("unknown visitors", () => {
  it("refreshes the visitor lists and calls onVisitor for a new visit; a review refreshes them too", () => {
    const client = testQueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const onVisitor = vi.fn();
    renderHook(() => useLiveEvents({ url: "ws://test/api/ws", createSocket: create, onVisitor }), {
      wrapper: withClient(client),
    });
    act(() => latest().open());
    const visit = { type: "unknown", id: 4, camera: "gate", closest_name: "Ayesha", score: 0.31 };
    act(() => latest().send(visit));
    expect(onVisitor).toHaveBeenCalledWith(visit);
    expect(invalidated(invalidate)).toEqual(['["unknowns"]']); // not the attendance queries: no event
    invalidate.mockClear();
    act(() =>
      latest().send({ type: "unknowns_changed", action: "review_unknown", target: "unknown:4", user: "guard" }),
    );
    expect(invalidated(invalidate)).toEqual(['["unknowns"]', '["features"]']);
    expect(onVisitor).toHaveBeenCalledTimes(1);
  });
});

describe("live decisions", () => {
  it("keeps the latest decisions per camera, replacing the previous list", () => {
    const { hook } = setup();
    act(() => latest().open());
    const decision = {
      time: "08:05:01",
      kind: "match",
      track: 3,
      name: "waqas",
      score: 0.71,
      second: 0.42,
      margin: 0.29,
      face_px: 150,
      liveness: 0.97,
      detail: "vote 1/3",
    };
    act(() => latest().send({ type: "decisions", camera: "entrance", id: 1, decisions: [decision] }));
    expect(hook.result.current.decisions.entrance).toEqual([decision]);
    const skip = { ...decision, kind: "skip", name: null, score: null, second: null, margin: null, face_px: 60 };
    act(() => latest().send({ type: "decisions", camera: "entrance", id: 1, decisions: [decision, skip] }));
    act(() => latest().send({ type: "decisions", camera: "exit", id: 2, decisions: [] }));
    expect(hook.result.current.decisions.entrance).toHaveLength(2);
    expect(hook.result.current.decisions.exit).toEqual([]);
  });
});
