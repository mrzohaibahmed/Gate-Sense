import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FiltersProvider } from "@/app/filters";
import { healthNotices, type Health } from "@/lib/health";
import { fakeApi, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes, testQueryClient } from "@/test/render";

import { OverviewPage, SystemNotice } from "./Overview";

const DENIED =
  "ProgrammingError: Cannot open backup device 'D:\\data\\backups\\x.bak.partial'. Operating system error 5(Access is denied.).";

const health = (extra: Partial<Health> = {}): Health => ({
  status: "ok",
  version: "0.1.0",
  started: "2026-10-24T08:00:00",
  uptime_seconds: 3600,
  database: { status: "ok", backend: "mssql", detail: null },
  cameras: [],
  disk: { path: "D:\\data", free_bytes: 1e11, total_bytes: 2e11, problem: null },
  backup: { last_run: null, ok: null, error: null, problem: null },
  problems: [],
  ...extra,
});

const failedBackup = (error: string) =>
  health({
    status: "degraded",
    backup: { last_run: "2026-10-31T08:56:00", ok: false, error, problem: `the last nightly backup failed: ${error}` },
    problems: [`the last nightly backup failed: ${error}`],
  });

describe("healthNotices", () => {
  it("says folder access denied only when the error does", () => {
    expect(healthNotices(failedBackup(DENIED))[0]).toMatchObject({
      title: "Nightly backup failed",
      detail: "Backup folder access denied",
      technical: DENIED,
    });
    expect(healthNotices(failedBackup("the disk is full"))[0]?.detail).toBe("See the details for the reason");
  });

  it("drops no problem it doesn't recognise", () => {
    const notices = healthNotices(health({ problems: ["something new"] }));
    expect(notices).toMatchObject([{ title: "System warning", technical: "something new" }]);
    expect(healthNotices(health())).toEqual([]);
  });
});

function camera(id: number, name: string, direction: "IN" | "OUT", state: string, events: number) {
  return { id, camera: name, direction, state, events, fps: null, last_error: null, warning: null, site_id: 1 };
}

const overview = (extra = {}) => ({
  date: "2026-11-02",
  shifts: false,
  people: 650,
  scheduled: 0,
  present: 604,
  inside_now: 527,
  complete: 77,
  on_time: 0,
  late: 0,
  early_leave: 0,
  overtime: 0,
  missing_in: 0,
  missing_out: 0,
  check: 0,
  absent: 0,
  not_in_yet: 0,
  off_day: 0,
  unscheduled: 0,
  arrivals_by_hour: Array.from({ length: 24 }, (_, hour) => ({ hour, arrivals: 0 })),
  cameras: [camera(1, "entrance", "IN", "running", 604), camera(2, "exit", "OUT", "stopped", 77)],
  ...extra,
});

const row = {
  date: "2026-11-02",
  person_id: 7,
  employee_code: "4425",
  name: "Zohaib Ahmed",
  department: "IT",
  site_id: 1,
  shift_id: null,
  shift: null,
  shift_start: null,
  shift_end: null,
  first_in: "2026-11-02T09:11:00",
  last_out: null,
  worked_hours: null,
  worked_minutes: null,
  late_minutes: null,
  early_leave_minutes: null,
  overtime_minutes: null,
  status: "missing OUT",
  inside: true,
  open: true,
};

function show(role: string, routes: Parameters<typeof fakeApi>[0] = {}) {
  const api = fakeApi({
    "GET /api/health": () => health(),
    "GET /api/overview": () => overview(),
    "GET /api/attendance": () => ({ items: [row], total: 604, page: 1, page_size: 5 }),
    "GET /api/events": () => ({ items: [], total: 0, page: 1, page_size: 5 }),
    "GET /api/sites": () => ({ items: [], total: 0, page: 1, page_size: 500 }),
    ...routes,
  });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
  const me = makeMe(role, PERMISSIONS[role]!);
  renderRoutes(
    [
      {
        path: "/",
        element: (
          <FiltersProvider>
            <OverviewPage />
          </FiltersProvider>
        ),
      },
    ],
    "/",
    me,
    testQueryClient(me),
  );
  return api;
}

afterEach(() => vi.unstubAllGlobals());

describe("SystemNotice", () => {
  function notice(role: string) {
    const api = fakeApi({ "GET /api/health": () => failedBackup(DENIED) });
    vi.stubGlobal("fetch", api.fetchMock);
    const me = makeMe(role, PERMISSIONS[role]!);
    renderRoutes([{ path: "/", element: <SystemNotice /> }], "/", me, testQueryClient(me));
  }

  it("is short, with the full error one click away for an administrator", async () => {
    notice("admin");
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("Nightly backup failed · Backup folder access denied");
    expect(banner).not.toHaveTextContent("ProgrammingError");
    await userEvent.click(within(banner).getByRole("button", { name: "View details" }));
    expect(await screen.findByText(DENIED)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings > System" })).toHaveAttribute(
      "href",
      "/settings?tab=system",
    );
  });

  it("keeps the technical text from other roles", async () => {
    notice("guard");
    await userEvent.click(within(await screen.findByRole("alert")).getByRole("button", { name: "View details" }));
    expect(await screen.findByText(/An administrator can see/)).toBeInTheDocument();
    expect(screen.queryByText(DENIED)).not.toBeInTheDocument();
  });

  it("shows nothing when all is well", async () => {
    const api = fakeApi({ "GET /api/health": () => health() });
    vi.stubGlobal("fetch", api.fetchMock);
    const me = makeMe("guard", PERMISSIONS.guard!);
    renderRoutes([{ path: "/", element: <SystemNotice /> }], "/", me, testQueryClient(me));
    await waitFor(() => expect(api.find("GET", "/api/health")).toHaveLength(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("OverviewPage", () => {
  it("shows the backend's figures and opens the list of everyone inside", async () => {
    show("admin");
    expect(await screen.findByTestId("inside-count")).toHaveTextContent("527");
    expect(screen.getByRole("heading", { name: "People in motion." })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View everyone inside" })).toHaveAttribute("href", "/attendance?inside=1");
    expect(screen.getByText("Search and filter 527 people")).toBeInTheDocument();
    expect(screen.getByText("604 / 650")).toBeInTheDocument();
    expect(screen.getByText("People with passes")).toBeInTheDocument();
    expect(screen.getByText("604 IN events today")).toBeInTheDocument();
    expect(screen.getByText("77 OUT events today")).toBeInTheDocument();
    expect(await screen.findByText("604 people")).toBeInTheDocument(); // the day's total, not the preview's size
    expect(screen.getByRole("link", { name: /Zohaib Ahmed/ })).toHaveAttribute("href", "/people/7");
  });

  it("shows no figures while loading, and an error instead of zeros", async () => {
    show("admin", {
      "GET /api/overview": () => {
        throw new Error("down");
      },
    });
    expect(screen.queryByTestId("inside-count")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/Attendance issues can't be shown/)).toBeInTheDocument());
    expect(screen.queryByTestId("inside-count")).not.toBeInTheDocument();
    expect(screen.queryByText("No attendance issues")).not.toBeInTheDocument();
  });

  it("is all clear only with every count at zero, and links the counts that aren't", async () => {
    show("admin");
    expect(await screen.findByText("No attendance issues")).toBeInTheDocument();
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
    show("admin", { "GET /api/overview": () => overview({ date: "2026-11-02", missing_out: 3 }) });
    const link = await screen.findByRole("link", { name: "Missing OUT: 3, open the records" });
    expect(link.getAttribute("href")).toContain("status=missing+OUT");
    expect(link.getAttribute("href")).toContain("open=false");
    expect(screen.queryByText("No attendance issues")).not.toBeInTheDocument();
  });

  it("has no count for a past day, and shows each camera of a group", async () => {
    show("guard", {
      "GET /api/overview": () =>
        overview({
          inside_now: null,
          cameras: [
            camera(1, "gate a", "IN", "running", 4),
            camera(2, "gate b", "IN", "stopped", 6),
            camera(3, "exit", "OUT", "running", 1),
          ],
        }),
    });
    expect(await screen.findByTestId("inside-count")).toHaveTextContent("—");
    expect(screen.getByText("Only available for today")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "View everyone inside" })).not.toBeInTheDocument();
    expect(screen.getByText("2 cameras")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 running")).toBeInTheDocument();
    expect(screen.getByText("gate a")).toBeInTheDocument();
    expect(screen.getByText("gate b")).toBeInTheDocument();
    expect(screen.getByText("10 IN events today")).toBeInTheDocument();
  });

  it("offers the cameras only to roles that may watch them", async () => {
    show("viewer");
    await screen.findByTestId("inside-count");
    expect(screen.queryByRole("link", { name: "Open cameras" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Zohaib Ahmed/ })).toBeInTheDocument();
  });

  it("says Present is of those rostered, with shifts on", async () => {
    show("admin", { "GET /api/overview": () => overview({ shifts: true, scheduled: 640, on_time: 500, late: 2 }) });
    expect(await screen.findByText("604 / 640")).toBeInTheDocument();
    expect(screen.getByText("Rostered on a shift")).toBeInTheDocument();
    expect(screen.queryByText("Complete")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Late: 2, open the records" })).toBeInTheDocument();
  });
});
