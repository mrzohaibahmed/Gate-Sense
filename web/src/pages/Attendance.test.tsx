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
  status: "present",
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
