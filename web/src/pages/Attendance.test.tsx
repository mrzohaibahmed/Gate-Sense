import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AttendanceRow } from "@/api/types";
import { routes } from "@/app/routes";
import { fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

const row = (over: Partial<AttendanceRow>): AttendanceRow => ({
  date: "2026-10-21",
  person_id: 1,
  employee_code: "1001",
  name: "Ayesha",
  department: "Stores",
  site_id: 1,
  first_in: "2026-10-21T08:05:00",
  last_out: "2026-10-21T17:10:00",
  worked_hours: 9.08,
  worked_minutes: 545,
  status: "complete",
  inside: false,
  ...over,
});

const ROWS = [
  row({}),
  row({
    person_id: 2,
    employee_code: "1002",
    name: "Bilal",
    department: "Office",
    last_out: null,
    worked_hours: null,
    worked_minutes: null,
    status: "missing exit",
  }),
];

let api: ReturnType<typeof fakeApi>;

beforeEach(() => {
  api = fakeApi({
    "GET /api/attendance": (url) => {
      const status = url.searchParams.get("status");
      const dept = url.searchParams.get("department");
      return page(ROWS.filter((r) => (!status || r.status === status) && (!dept || r.department === dept)));
    },
    "GET /api/people": () =>
      page([
        {
          id: 1,
          employee_code: "1001",
          name: "Ayesha",
          department: "Stores",
          shift: null,
          active: true,
          site_id: 1,
          site: "Main",
          photos: 0,
          photo_url: null,
          embeddings: 0,
        },
        {
          id: 2,
          employee_code: "1002",
          name: "Bilal",
          department: "Office",
          shift: null,
          active: true,
          site_id: 1,
          site: "Main",
          photos: 0,
          photo_url: null,
          embeddings: 0,
        },
      ]),
    "GET /api/events": () =>
      page([
        {
          id: 11,
          timestamp: "2026-10-21T08:05:00",
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
        },
      ]),
  });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
});
afterEach(() => vi.unstubAllGlobals());

const lastQuery = () => api.find("GET", "/api/attendance").at(-1)!.url.searchParams;

describe("attendance table", () => {
  it("shows the rows and sends each filter to the API", async () => {
    const user = userEvent.setup();
    const { router } = renderRoutes(routes, "/attendance?from=2026-10-21&to=2026-10-21", makeMe("hr", PERMISSIONS.hr!));
    expect(await screen.findByText("Bilal")).toBeInTheDocument();
    expect(screen.getByText("Ayesha")).toBeInTheDocument();
    expect(lastQuery().get("from")).toBe("2026-10-21");
    expect(lastQuery().get("sort")).toBe("-date");

    await user.selectOptions(screen.getByLabelText("Status"), "missing exit");
    await waitFor(() => expect(lastQuery().get("status")).toBe("missing exit"));
    await waitFor(() => expect(screen.queryByText("Ayesha")).not.toBeInTheDocument());
    expect(screen.getByText("Bilal")).toBeInTheDocument();
    expect(router.state.location.search).toContain("status=missing+exit");

    await user.selectOptions(screen.getByLabelText("Status"), "");
    await user.selectOptions(screen.getByLabelText("Department"), "Stores");
    await waitFor(() => expect(lastQuery().get("department")).toBe("Stores"));
    expect(lastQuery().get("status")).toBeNull();

    await user.type(screen.getByLabelText("Search name or code"), "ayes{Enter}");
    await waitFor(() => expect(lastQuery().get("q")).toBe("ayes"));

    await user.click(screen.getByRole("button", { name: "Sort by Name" }));
    await waitFor(() => expect(lastQuery().get("sort")).toBe("name"));
  });

  it("the date range goes to the API", async () => {
    renderRoutes(routes, "/attendance?from=2026-10-21&to=2026-10-21", makeMe("viewer", PERMISSIONS.viewer!));
    await screen.findByText("Ayesha");
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-10-01" } });
    await waitFor(() => expect(lastQuery().get("from")).toBe("2026-10-01"));
    expect(lastQuery().get("to")).toBe("2026-10-21");
  });

  it("a row opens the day's events; the correction form only for HR and admin", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/attendance?from=2026-10-21", makeMe("hr", PERMISSIONS.hr!));
    await user.click(await screen.findByRole("row", { name: "Ayesha, 2026-10-21" }));
    const panel = await screen.findByRole("dialog");
    expect(within(panel).getByRole("heading", { name: "Ayesha" })).toBeInTheDocument();
    expect(await within(panel).findByText("entrance · score 0.71")).toBeInTheDocument();
    expect(within(panel).getByRole("form", { name: "Add correction" })).toBeInTheDocument();
    const events = api.find("GET", "/api/events").at(-1)!.url.searchParams;
    expect([events.get("person"), events.get("from"), events.get("to")]).toEqual(["1", "2026-10-21", "2026-10-21"]);
  });

  it("a viewer sees the events but no correction form", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/attendance?from=2026-10-21", makeMe("viewer", PERMISSIONS.viewer!));
    await user.click(await screen.findByRole("row", { name: "Ayesha, 2026-10-21" }));
    const panel = await screen.findByRole("dialog");
    await within(panel).findByText("entrance · score 0.71");
    expect(within(panel).queryByRole("form", { name: "Add correction" })).not.toBeInTheDocument();
  });
});

describe("void and restore", () => {
  const ev = (over: Record<string, unknown>) => ({
    id: 11,
    timestamp: "2026-10-21T08:05:00",
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
    ...over,
  });

  beforeEach(() => {
    api = fakeApi({
      "GET /api/attendance": () => page(ROWS),
      "GET /api/events": () =>
        page([
          ev({}),
          ev({ id: 12, timestamp: "2026-10-21T17:10:00", direction: "OUT", camera: "exit", voided: true }),
        ]),
      "POST /api/attendance/corrections": () => ({ ok: true }),
    });
    vi.stubGlobal("fetch", api.fetchMock);
  });

  it("HR voids an event and restores a voided one, each with a reason", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/attendance?from=2026-10-21", makeMe("hr", PERMISSIONS.hr!));
    await user.click(await screen.findByRole("row", { name: "Ayesha, 2026-10-21" }));
    const panel = await screen.findByRole("dialog");
    const voided = await within(panel).findByRole("listitem", { name: "OUT at 17:10 (voided)" });
    expect(within(voided).getByText("Voided")).toBeInTheDocument();
    expect(voided.className).toContain("opacity-60");

    const live = within(panel).getByRole("listitem", { name: "IN at 08:05" });
    await user.click(within(live).getByRole("button", { name: "Void" }));
    let dialog = await screen.findByRole("dialog", { name: "Void this event?" });
    await user.click(within(dialog).getByRole("button", { name: "Void" }));
    expect(await within(dialog).findByText(/A reason is required/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Reason (required)"), "walked past, didn't come in");
    await user.click(within(dialog).getByRole("button", { name: "Void" }));
    await waitFor(() => expect(api.find("POST", "/api/attendance/corrections")).toHaveLength(1));
    expect(api.find("POST", "/api/attendance/corrections")[0]!.body).toEqual({
      action: "void",
      event_id: 11,
      reason: "walked past, didn't come in",
    });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Void this event?" })).not.toBeInTheDocument());

    await user.click(within(voided).getByRole("button", { name: "Restore" }));
    dialog = await screen.findByRole("dialog", { name: "Restore this event?" });
    await user.type(within(dialog).getByLabelText("Reason (required)"), "voided by mistake");
    await user.click(within(dialog).getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(api.find("POST", "/api/attendance/corrections")).toHaveLength(2));
    expect(api.find("POST", "/api/attendance/corrections")[1]!.body).toEqual({
      action: "restore",
      event_id: 12,
      reason: "voided by mistake",
    });
  });

  it("voided events can be hidden; a viewer has no void buttons", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/attendance?from=2026-10-21", makeMe("viewer", PERMISSIONS.viewer!));
    await user.click(await screen.findByRole("row", { name: "Ayesha, 2026-10-21" }));
    const panel = await screen.findByRole("dialog");
    await within(panel).findByRole("listitem", { name: "OUT at 17:10 (voided)" });
    expect(within(panel).queryByRole("button", { name: "Void" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Restore" })).not.toBeInTheDocument();

    await user.click(within(panel).getByRole("switch", { name: "Show voided events" }));
    expect(within(panel).queryByRole("listitem", { name: "OUT at 17:10 (voided)" })).not.toBeInTheDocument();
    expect(within(panel).getByRole("listitem", { name: "IN at 08:05" })).toBeInTheDocument();
  });
});

describe("repeats and check", () => {
  it("greys the repeat events of the day and tags them; a check day shows its badge", async () => {
    const user = userEvent.setup();
    const ev = (id: number, time: string, direction: string, repeat: boolean) => ({
      id,
      timestamp: `2026-10-21T${time}`,
      original_timestamp: null,
      person_id: 1,
      employee_code: "1001",
      name: "Ayesha",
      camera_id: direction === "IN" ? 1 : 2,
      camera: direction === "IN" ? "entrance" : "exit",
      direction,
      site_id: 1,
      score: 0.7,
      manual: false,
      voided: false,
      repeat,
      snapshot_url: null,
    });
    api = fakeApi({
      "GET /api/attendance": () =>
        page([row({}), row({ person_id: 2, employee_code: "1002", name: "Bilal", status: "check" })]),
      "GET /api/events": () =>
        page([ev(1, "08:00:00", "IN", false), ev(2, "16:00:00", "OUT", false), ev(3, "17:00:00", "IN", true)]),
    });
    vi.stubGlobal("fetch", api.fetchMock);
    renderRoutes(routes, "/attendance?from=2026-10-21", makeMe("viewer", PERMISSIONS.viewer!));
    expect(await screen.findByText("Check")).toBeInTheDocument();
    await user.click(screen.getByRole("row", { name: "Ayesha, 2026-10-21" }));
    const panel = await screen.findByRole("dialog");
    const repeat = await within(panel).findByRole("listitem", { name: "IN at 17:00 (repeat)" });
    expect(within(repeat).getByText("repeat")).toBeInTheDocument();
    expect(repeat.className).toContain("opacity-60");
    const used = within(panel).getByRole("listitem", { name: "OUT at 16:00" });
    expect(used.className).not.toContain("opacity-60");
    expect(within(used).queryByText("repeat")).not.toBeInTheDocument();
  });
});
