import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RosterImportPreview, RosterRow, Shift } from "@/api/types";
import { routes } from "@/app/routes";
import { fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

const shift = (id: number, name: string, start: string, end: string): Shift => ({
  id,
  name,
  start,
  end,
  crosses_midnight: end <= start,
  grace_min: 30,
  in_window_before_min: 120,
  out_window_after_min: 240,
  effective_from: null,
  versions: [],
  roster_days: 0,
});
const SHIFTS = [
  shift(1, "Morning", "06:30", "14:30"),
  shift(2, "General", "08:00", "16:30"),
  shift(3, "Night", "22:00", "06:00"),
];

const person = (person_id: number, name: string, cells: RosterRow["cells"]): RosterRow => ({
  person_id,
  employee_code: String(1000 + person_id),
  name,
  department: "Stores",
  site_id: 1,
  active: true,
  cells,
});
const WEEK = [
  person(1, "Ayesha", [
    { date: "2026-10-19", shift_id: 2 },
    { date: "2026-10-25", shift_id: null },
  ]),
  person(2, "Bilal", []),
];
const LAST_WEEK = [person(2, "Bilal", [{ date: "2026-10-13", shift_id: 1 }])];

function start(role: "hr" | "viewer" | "guard", preview?: RosterImportPreview) {
  const api = fakeApi({
    "GET /api/shifts": () => page(SHIFTS),
    "GET /api/roster": (url) => page(url.searchParams.get("from") === "2026-10-12" ? LAST_WEEK : WEEK),
    "PUT /api/roster": (_url, body) => ({ changed: (body as { entries: unknown[] }).entries.length, removed: 0 }),
    "POST /api/roster/import/preview": () => preview,
  });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
  renderRoutes(routes, "/roster?week=2026-10-21", makeMe(role, PERMISSIONS[role]!));
  return api;
}

async function saveWith(user: ReturnType<typeof userEvent.setup>, reason: string) {
  await user.click(screen.getByRole("button", { name: /Save changes/ }));
  const dialog = await screen.findByRole("dialog");
  await user.type(within(dialog).getByLabelText(/Reason/), reason);
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
}

afterEach(() => vi.unstubAllGlobals());

describe("roster", () => {
  it("a viewer reads the week (Monday first) without any controls; a guard can't open it", async () => {
    const api = start("viewer");
    const ayesha = await screen.findByRole("row", { name: "Ayesha" });
    const cells = within(ayesha).getAllByRole("cell");
    expect(cells[1]).toHaveTextContent("General");
    expect(cells[7]).toHaveTextContent("OFF");
    expect(within(screen.getByRole("row", { name: "Bilal" })).getAllByText("—")).toHaveLength(7);
    expect(screen.queryByRole("button", { name: /Save changes/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /Ayesha on/ })).not.toBeInTheDocument();
    const query = api.find("GET", "/api/roster")[0]!.url.searchParams;
    expect([query.get("from"), query.get("to")]).toEqual(["2026-10-19", "2026-10-25"]);
    expect(screen.getByLabelText("Shifts")).toHaveTextContent("Night 22:00–06:00 (next day)");
  });

  it("a guard gets the 403 page", async () => {
    start("guard");
    expect(await screen.findByText("Not for your role")).toBeInTheDocument();
  });

  it("HR changes cells, sees them unsaved, and saves them together with one reason", async () => {
    const user = userEvent.setup();
    const api = start("hr");
    await user.selectOptions(await screen.findByRole("combobox", { name: "Bilal on 2026-10-19" }), "3");
    await user.selectOptions(screen.getByRole("combobox", { name: "Ayesha on 2026-10-25" }), "");
    await user.selectOptions(screen.getByRole("combobox", { name: "Ayesha on 2026-10-19" }), "2"); // unchanged
    expect(screen.getByText("2 unsaved")).toBeInTheDocument();
    await saveWith(user, "week plan");
    await waitFor(() => expect(api.find("PUT", "/api/roster")).toHaveLength(1));
    expect(api.find("PUT", "/api/roster")[0]!.body).toEqual({
      reason: "week plan",
      entries: [
        { person_id: 2, date: "2026-10-19", shift_id: 3, clear: false },
        { person_id: 1, date: "2026-10-25", shift_id: null, clear: true },
      ],
    });
  });

  it("assigns a shift to the selected people and days at once", async () => {
    const user = userEvent.setup();
    const api = start("hr");
    await user.click(await screen.findByRole("checkbox", { name: "Select everyone" }));
    await user.click(screen.getByRole("checkbox", { name: "Select 2026-10-20" }));
    await user.click(screen.getByRole("checkbox", { name: "Select 2026-10-21" }));
    expect(screen.getByLabelText("Set selected cells")).toHaveTextContent("Selected: 2 people × 2 days");
    await user.selectOptions(screen.getByRole("combobox", { name: "Value for the selected cells" }), "OFF");
    await user.click(screen.getByRole("button", { name: "Apply to selected" }));
    expect(screen.getByRole("combobox", { name: "Bilal on 2026-10-21" })).toHaveValue("OFF");
    await saveWith(user, "days off");
    await waitFor(() => expect(api.find("PUT", "/api/roster")).toHaveLength(1));
    const body = api.find("PUT", "/api/roster")[0]!.body as { entries: { shift_id: number | null; clear: boolean }[] };
    expect(body.entries).toHaveLength(4);
    expect(body.entries.every((e) => e.shift_id === null && !e.clear)).toBe(true);
  });

  it("copies last week's entries into this week for review", async () => {
    const user = userEvent.setup();
    const api = start("hr");
    await screen.findByRole("row", { name: "Bilal" });
    await user.click(screen.getByRole("button", { name: /Copy last week/ }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Bilal on 2026-10-20" })).toHaveValue("1"));
    expect(api.find("GET", "/api/roster").some((c) => c.url.searchParams.get("to") === "2026-10-18")).toBe(true);
    expect(screen.getByText("1 unsaved")).toBeInTheDocument();
  });

  it("the Excel import lists every error and won't import until the file is fixed", async () => {
    const user = userEvent.setup();
    const api = start("hr", {
      file: "week.xlsx",
      dates: ["2026-10-19", "2026-10-20"],
      people: 1,
      ignored_columns: [],
      counts: { set: 1, unchanged: 0, error: 1 },
      changes: [
        {
          line: 2,
          column: "2026-10-19",
          employee_code: "1001",
          name: "Ayesha",
          date: "2026-10-19",
          old: null,
          new: "Morning",
        },
      ],
      errors: [{ line: 3, column: "2026-10-20", employee_code: "1002", message: "no shift called 'Lunch'" }],
    });
    await user.click(await screen.findByRole("button", { name: /Import from Excel/ }));
    const dialog = await screen.findByRole("dialog");
    const template = within(dialog)
      .getByRole("link", { name: /This week \(Excel\)/ })
      .getAttribute("href")!;
    expect(template).toContain("from=2026-10-19");
    expect(template).toContain("to=2026-10-25");
    await user.upload(within(dialog).getByLabelText("File"), new File(["x"], "week.xlsx"));
    expect(await within(dialog).findByText("no shift called 'Lunch'")).toBeInTheDocument();
    expect(within(dialog).getByRole("table", { name: "Changes" })).toHaveTextContent("Morning");
    expect(within(dialog).getByRole("button", { name: /Import 1 entry/ })).toBeDisabled();
    expect(api.find("POST", "/api/roster/import")).toHaveLength(0);
  });
});
