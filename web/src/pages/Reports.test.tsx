import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DailyRow, PersonTotal } from "@/api/types";
import { routes } from "@/app/routes";
import { fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

import { presetRange } from "@/lib/ranges";

const daily: DailyRow[] = [
  {
    date: "2026-10-21",
    employee_code: "1001",
    name: "Ayesha",
    department: "Stores",
    shift: "General",
    shift_start: "2026-10-21T08:00:00",
    shift_end: "2026-10-21T16:30:00",
    first_in: "2026-10-21T08:05:00",
    last_out: "2026-10-21T17:10:00",
    worked_hours: 9.08,
    late_minutes: 0,
    early_leave_minutes: 0,
    overtime_minutes: 40,
    status: "on time",
    open: false,
  },
  {
    date: "2026-10-21",
    employee_code: "1002",
    name: "Bilal",
    department: "Office",
    shift: "General",
    shift_start: "2026-10-21T08:00:00",
    shift_end: "2026-10-21T16:30:00",
    first_in: "2026-10-21T09:00:00",
    last_out: null,
    worked_hours: null,
    late_minutes: 60,
    early_leave_minutes: null,
    overtime_minutes: null,
    status: "missing OUT",
    open: false,
  },
];
const totals: PersonTotal[] = [
  {
    employee_code: "1001",
    name: "Ayesha",
    department: "Stores",
    days: 23,
    days_scheduled: 21,
    days_present: 20,
    days_on_time: 17,
    days_late: 2,
    late_minutes: 45,
    days_early_leave: 0,
    early_leave_minutes: 0,
    overtime_minutes: 90,
    days_missing_in: 0,
    days_missing_out: 1,
    days_check: 0,
    days_absent: 1,
    days_not_in_yet: 0,
    days_off: 2,
    days_unscheduled: 0,
    total_hours: 172.5,
    avg_hours: 9.08,
  },
];

let api: ReturnType<typeof fakeApi>;

beforeEach(() => {
  api = fakeApi({
    "GET /api/reports/daily": (url) => ({
      first: url.searchParams.get("from"),
      last: url.searchParams.get("to"),
      rows: daily.filter(
        (r) => !url.searchParams.get("department") || r.department === url.searchParams.get("department"),
      ),
    }),
    "GET /api/reports/monthly": (url) => ({
      first: url.searchParams.get("from"),
      last: url.searchParams.get("to"),
      people: totals,
    }),
    "GET /api/people": () => page([]),
  });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
});
afterEach(() => vi.unstubAllGlobals());

describe("presetRange", () => {
  it("gives the first and last day of each preset", () => {
    expect(presetRange("today", "2026-10-22")).toEqual(["2026-10-22", "2026-10-22"]);
    expect(presetRange("week", "2026-10-22")).toEqual(["2026-10-19", "2026-10-22"]); // Thursday -> Monday
    expect(presetRange("week", "2026-10-19")).toEqual(["2026-10-19", "2026-10-19"]);
    expect(presetRange("week", "2026-10-25")).toEqual(["2026-10-19", "2026-10-25"]); // Sunday
    expect(presetRange("month", "2026-10-22")).toEqual(["2026-10-01", "2026-10-22"]);
    expect(presetRange("last-month", "2026-10-22")).toEqual(["2026-09-01", "2026-09-30"]);
    expect(presetRange("last-month", "2026-01-15")).toEqual(["2025-12-01", "2025-12-31"]);
    expect(presetRange("last-month", "2028-03-31")).toEqual(["2028-02-01", "2028-02-29"]);
  });
});

const lastCall = (path: string) => api.find("GET", path).at(-1)!.url.searchParams;

describe("reports page", () => {
  it("shows the daily register with missing OUT, and sends the range and department", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/reports?range=custom&from=2026-10-21&to=2026-10-21", makeMe("viewer", PERMISSIONS.viewer!));
    const table = await screen.findByRole("table", { name: "Daily register" });
    expect(within(table).getByText("Ayesha")).toBeInTheDocument();
    expect(within(table).getByText("9.08")).toBeInTheDocument();
    expect(within(table).getByText("Missing OUT")).toBeInTheDocument();
    expect([lastCall("/api/reports/daily").get("from"), lastCall("/api/reports/daily").get("to")]).toEqual([
      "2026-10-21",
      "2026-10-21",
    ]);

    await user.selectOptions(screen.getByLabelText("Department"), "Office");
    await waitFor(() => expect(lastCall("/api/reports/daily").get("department")).toBe("Office"));
    await waitFor(() => expect(within(table).queryByText("Ayesha")).not.toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-10-01" } });
    await waitFor(() => expect(lastCall("/api/reports/daily").get("from")).toBe("2026-10-01"));
  });

  it("the monthly summary shows totals per person; exports carry the same filters", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/reports?range=custom&from=2026-10-01&to=2026-10-21", makeMe("hr", PERMISSIONS.hr!));
    await screen.findByRole("table", { name: "Daily register" });
    await user.click(screen.getByRole("tab", { name: "Monthly summary" }));
    const table = await screen.findByRole("table", { name: "Monthly summary" });
    expect(within(table).getByText("20 / 21")).toBeInTheDocument();
    expect(within(table).getByText("172.50")).toBeInTheDocument();
    // switching report resets the range to this month
    expect(lastCall("/api/reports/monthly").get("to")).toBe(presetRange("month")[1]);
    expect(lastCall("/api/reports/monthly").get("from")).toBe(presetRange("month")[0]);

    const excel = new URL(screen.getByRole("link", { name: /Export Excel/ }).getAttribute("href")!, "http://x");
    expect(excel.pathname).toBe("/api/reports/monthly/export");
    expect(excel.searchParams.get("format")).toBe("xlsx");
    expect(excel.searchParams.get("from")).toBe(presetRange("month")[0]);
    const csv = new URL(screen.getByRole("link", { name: /Export CSV/ }).getAttribute("href")!, "http://x");
    expect(csv.searchParams.get("format")).toBe("csv");
  });

  it("the range presets set the dates", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/reports", makeMe("admin", PERMISSIONS.admin!));
    await screen.findByRole("table", { name: "Daily register" });
    await user.selectOptions(screen.getByLabelText("Range"), "last-month");
    const [first, last] = presetRange("last-month");
    await waitFor(() => expect(lastCall("/api/reports/daily").get("from")).toBe(first));
    expect(lastCall("/api/reports/daily").get("to")).toBe(last);
    expect(screen.getByLabelText("From")).toHaveValue(first);
  });
});
