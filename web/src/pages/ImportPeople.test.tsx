import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ImportPreview } from "@/api/types";
import { routes } from "@/app/routes";
import { fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

const row = (over: Partial<ImportPreview["rows"][number]>): ImportPreview["rows"][number] => ({
  line: 2,
  employee_code: "1001",
  name: "Ayesha",
  department: "Stores",
  shift: null,
  site: null,
  action: "unchanged",
  person_id: 1,
  changes: {},
  errors: [],
  notes: [],
  ...over,
});

function preview(rows: ImportPreview["rows"]): ImportPreview {
  const count = (a: string) => rows.filter((r) => r.action === a).length;
  return {
    file: "staff.xlsx",
    columns: ["employee_code", "name", "department"],
    ignored_columns: ["Remarks"],
    counts: { new: count("new"), update: count("update"), unchanged: count("unchanged"), error: count("error") },
    rows,
  };
}

const GOOD = preview([
  row({}),
  row({
    line: 3,
    employee_code: "1002",
    name: "Bilal Ahmed",
    action: "update",
    changes: { department: ["Office", "Accounts"] },
  }),
  row({ line: 4, employee_code: "2001", name: "Zara", department: "Gate", action: "new", person_id: null }),
]);
const BAD = preview([
  row({ line: 2, employee_code: "2001", name: "Zara", action: "new", person_id: null }),
  row({ line: 3, employee_code: "2002", name: "", action: "error", person_id: null, errors: ["no name"] }),
]);

function start(result: ImportPreview) {
  const api = fakeApi({
    "GET /api/people": () => page([]),
    "GET /api/sites": () => page([{ id: 1, name: "Main" }]),
    "POST /api/people/import/preview": () => result,
    "POST /api/people/import": () => ({ file: "staff.xlsx", added: 1, updated: 1, unchanged: 1 }),
  });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
  renderRoutes(routes, "/people", makeMe("hr", PERMISSIONS.hr!));
  return api;
}

async function chooseFile(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Import from file" }));
  const dialog = await screen.findByRole("dialog");
  const file = new File(["fake workbook"], "staff.xlsx", {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  await user.upload(within(dialog).getByLabelText("File"), file);
  return dialog;
}

afterEach(() => vi.unstubAllGlobals());

describe("import people from a file", () => {
  it("shows what the file would do, then imports it with a reason", async () => {
    const user = userEvent.setup();
    const api = start(GOOD);
    const dialog = await chooseFile(user);
    expect(await within(dialog).findByText("1 new")).toBeInTheDocument();
    expect(within(dialog).getByText("1 to update")).toBeInTheDocument();
    expect(within(dialog).getByText(/Not imported \(unknown columns\): Remarks/)).toBeInTheDocument();
    expect(api.find("POST", "/api/people/import/preview")[0]!.url.searchParams.get("filename")).toBe("staff.xlsx");

    const update = within(dialog).getByRole("row", { name: "Line 3" });
    expect(update).toHaveTextContent("Update");
    expect(update).toHaveTextContent("Department: Office → Accounts");
    expect(within(dialog).queryByRole("row", { name: "Line 2" })).not.toBeInTheDocument(); // unchanged: hidden
    await user.click(within(dialog).getByRole("switch", { name: "Show unchanged rows" }));
    expect(within(dialog).getByRole("row", { name: "Line 2" })).toHaveTextContent("Unchanged");

    await user.click(within(dialog).getByRole("button", { name: "Import 2 people" }));
    expect(await within(dialog).findByText(/A reason is required/)).toBeInTheDocument();
    expect(api.find("POST", "/api/people/import")).toHaveLength(0);
    await user.type(within(dialog).getByLabelText("Reason (required)"), "HR list of October");
    await user.click(within(dialog).getByRole("button", { name: "Import 2 people" }));
    await waitFor(() => expect(api.find("POST", "/api/people/import")).toHaveLength(1));
    const sent = api.find("POST", "/api/people/import")[0]!;
    expect(sent.url.searchParams.get("reason")).toBe("HR list of October");
    expect(sent.url.searchParams.get("filename")).toBe("staff.xlsx");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("a file with errors can't be imported: the bad rows are shown", async () => {
    const user = userEvent.setup();
    start(BAD);
    const dialog = await chooseFile(user);
    expect(await within(dialog).findByText("1 with errors")).toBeInTheDocument();
    expect(within(dialog).getByRole("row", { name: "Line 3" })).toHaveTextContent("no name");
    expect(within(dialog).getByText(/nothing is imported while any row has an error/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Import 1 person" })).toBeDisabled();
  });

  it("offers the templates", async () => {
    const user = userEvent.setup();
    start(GOOD);
    await user.click(await screen.findByRole("button", { name: "Import from file" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("link", { name: /Template \(Excel\)/ })).toHaveAttribute(
      "href",
      "/api/people/import/template?format=xlsx",
    );
    expect(within(dialog).getByRole("link", { name: /Template \(CSV\)/ })).toHaveAttribute(
      "href",
      "/api/people/import/template?format=csv",
    );
  });

  it("a viewer has no import button", async () => {
    vi.stubGlobal("fetch", fakeApi({ "GET /api/people": () => page([]) }).fetchMock);
    vi.stubGlobal("WebSocket", SilentSocket);
    renderRoutes(routes, "/people", makeMe("viewer", PERMISSIONS.viewer!));
    await screen.findByRole("heading", { name: "People" });
    expect(screen.queryByRole("button", { name: "Import from file" })).not.toBeInTheDocument();
  });
});
