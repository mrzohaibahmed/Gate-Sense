import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UnknownVisit } from "@/api/types";
import { routes } from "@/app/routes";
import { fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

const visit = (over: Partial<UnknownVisit> = {}): UnknownVisit => ({
  id: 7,
  camera_id: 1,
  camera: "gate",
  direction: "IN",
  site_id: 1,
  first_seen: "2026-10-30T09:00:00",
  last_seen: "2026-10-30T09:02:00",
  tracks: 2,
  score: 0.31,
  closest_code: "1001",
  closest_name: "Ayesha",
  liveness: 0.93,
  face_px: 160,
  snapshot_url: "/api/unknowns/7/snapshot",
  status: "new",
  person_id: null,
  person_code: null,
  person_name: null,
  note: null,
  reviewed: null,
  reviewed_by: null,
  ...over,
});

const on = { enabled: true, updated: null, updated_by: null };
const off = { enabled: false, updated: null, updated_by: null };

function setup(role: string, handlers: Parameters<typeof fakeApi>[0] = {}, path = "/visitors") {
  const api = fakeApi({
    "GET /api/settings/unknowns": () => on,
    "GET /api/unknowns/counts": () => ({ enabled: true, new: 1, today: 1 }),
    "GET /api/unknowns": () => page([visit()]),
    ...handlers,
  });
  vi.stubGlobal("fetch", api.fetchMock);
  renderRoutes(routes, path, makeMe(role, PERMISSIONS[role]!));
  return api;
}

beforeEach(() => vi.stubGlobal("WebSocket", SilentSocket));
afterEach(() => vi.unstubAllGlobals());

describe("Unknown visitors page", () => {
  it("lists visits for a guard: snapshot, times, passes, camera, closest person, status", async () => {
    const api = setup("guard");
    const row = await screen.findByRole("row", { name: "Visitor at gate, 2026-10-30 09:00" });
    expect(within(row).getByRole("img")).toHaveAttribute("src", "/api/unknowns/7/snapshot");
    expect(within(row).getByText("–09:02")).toBeInTheDocument();
    expect(within(row).getByText("(2 passes)")).toBeInTheDocument();
    expect(within(row).getByText("Ayesha")).toBeInTheDocument();
    expect(within(row).getByText("0.31")).toBeInTheDocument();
    expect(within(row).getByText("New")).toBeInTheDocument();
    const query = api.find("GET", "/api/unknowns")[0]!.url.searchParams;
    expect(query.get("sort")).toBe("-first_seen");
    expect(query.get("from")).toBeTruthy(); // this week by default
    expect(screen.queryByText(/The visitor log is off/)).not.toBeInTheDocument(); // the log is on: no notice
  });

  it("filters by status", async () => {
    const api = setup("guard");
    const user = userEvent.setup();
    await screen.findByRole("row", { name: /Visitor at gate/ });
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "dismissed");
    await waitFor(() =>
      expect(api.find("GET", "/api/unknowns").at(-1)!.url.searchParams.get("status")).toBe("dismissed"),
    );
  });

  it("lets a guard dismiss a visit with a note and a reason, but not identify it", async () => {
    const api = setup("guard", {
      "PATCH /api/unknowns/7": () => visit({ status: "dismissed", note: "courier", reviewed_by: "guard" }),
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("row", { name: /Visitor at gate/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Unknown visitor")).toBeInTheDocument();
    const status = within(dialog).getByRole("combobox", { name: "Status" });
    expect(within(status).queryByRole("option", { name: "Identified as…" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("link", { name: /Enroll them/ })).not.toBeInTheDocument();
    await user.selectOptions(status, "dismissed");
    await user.type(within(dialog).getByRole("textbox", { name: "Note" }), "courier");
    await user.click(within(dialog).getByRole("button", { name: "Save review" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("A reason is required");
    expect(api.find("PATCH", "/api/unknowns/7")).toHaveLength(0);
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (required)" }), "checked at the gate");
    await user.click(within(dialog).getByRole("button", { name: "Save review" }));
    await waitFor(() => expect(api.find("PATCH", "/api/unknowns/7")).toHaveLength(1));
    expect(api.find("PATCH", "/api/unknowns/7")[0]!.body).toEqual({
      status: "dismissed",
      person_id: null,
      note: "courier",
      reason: "checked at the gate",
    });
    expect(api.find("GET", "/api/people")).toHaveLength(0); // guards can't list people
  });

  it("lets an admin identify a visit as a person and offers the enrollment wizard", async () => {
    const api = setup("admin", {
      "GET /api/people": () => page([{ id: 3, name: "Bilal", employee_code: "1002" }]),
      "PATCH /api/unknowns/7": () => visit({ status: "identified", person_id: 3, person_name: "Bilal" }),
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("row", { name: /Visitor at gate/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("link", { name: /Enroll them/ })).toHaveAttribute("href", "/enrollment");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Status" }), "identified");
    const who = within(dialog).getByRole("combobox", { name: "Who it was" });
    await within(who).findByRole("option", { name: "Bilal (1002)" });
    await user.click(within(dialog).getByRole("button", { name: "Save review" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Pick who it was");
    await user.selectOptions(who, "3");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (required)" }), "it was Bilal");
    await user.click(within(dialog).getByRole("button", { name: "Save review" }));
    await waitFor(() => expect(api.find("PATCH", "/api/unknowns/7")).toHaveLength(1));
    expect(api.find("PATCH", "/api/unknowns/7")[0]!.body).toMatchObject({ status: "identified", person_id: 3 });
  });

  it("says when the log is off (an admin is told where to switch it on)", async () => {
    setup("admin", { "GET /api/settings/unknowns": () => off });
    expect(await screen.findByText(/The visitor log is off/)).toHaveTextContent("Switch it on in Settings > System.");
  });
});

describe("Unknown visitors in the menu", () => {
  const menu = () => screen.findByRole("complementary", { name: "Main menu" });

  it("is there for a guard while the log is on, with the visits waiting for review", async () => {
    setup("guard", {}, "/live");
    const link = await within(await menu()).findByRole("link", { name: /Unknown visitors/ });
    expect(await within(link).findByLabelText("1 not reviewed")).toHaveTextContent("1");
  });

  it("is hidden while the log is off and nothing waits", async () => {
    setup(
      "guard",
      {
        "GET /api/settings/unknowns": () => off,
        "GET /api/unknowns/counts": () => ({ enabled: false, new: 0, today: 0 }),
      },
      "/live",
    );
    await within(await menu()).findByRole("link", { name: /Live cameras/ });
    expect(within(await menu()).queryByRole("link", { name: /Unknown visitors/ })).not.toBeInTheDocument();
  });

  it("stays while visits recorded before the log was switched off wait for review", async () => {
    setup(
      "guard",
      {
        "GET /api/settings/unknowns": () => off,
        "GET /api/unknowns/counts": () => ({ enabled: false, new: 2, today: 0 }),
      },
      "/live",
    );
    expect(await within(await menu()).findByRole("link", { name: /Unknown visitors/ })).toBeInTheDocument();
  });

  it("is never there for HR, who never ask for it", async () => {
    const api = setup("hr", {}, "/live");
    await within(await menu()).findByRole("link", { name: /Live cameras/ });
    expect(within(await menu()).queryByRole("link", { name: /Unknown visitors/ })).not.toBeInTheDocument();
    expect(api.find("GET", "/api/settings/unknowns")).toHaveLength(0);
    expect(api.find("GET", "/api/unknowns/counts")).toHaveLength(0);
  });
});
