import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routes } from "@/app/routes";
import { fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

const SYSTEM = {
  version: "0.1.0",
  started: "2026-10-26T08:00:00",
  uptime_seconds: 3600,
  python: "3.11",
  database_bytes: 1000,
  database_schema: 7,
  events: 0,
  people: 0,
  gallery_bytes: 0,
  enrolled: 0,
  backups: [],
  cameras_running: 0,
  device: { active: "cpu", label: "CPU", requested: "auto", providers: ["CPUExecutionProvider"], warning: null },
  camera_devices: [] as { camera: string; label: string; warning: string | null }[],
  maintenance: {
    last_run: null,
    last_ok: null,
    last_error: null,
    backup_hour: 2,
    backup_keep: 14,
    backup_copy_dir: null,
    snapshot_retention_days: 30,
    snapshots_bytes: 0,
  },
};

function start(role: string, path: string, enabled: boolean) {
  const api = fakeApi({
    "GET /api/settings/shifts": () => ({ enabled, updated: null, updated_by: null }),
    "PUT /api/settings/shifts": (_url, body) => ({ ...(body as object), updated: null, updated_by: "admin" }),
    "GET /api/system": () => SYSTEM,
    "GET /api/settings": () => ({ settings: [] }),
    "GET /api/attendance": () => page([]),
  });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
  renderRoutes(routes, path, makeMe(role, PERMISSIONS[role]!));
  return api;
}

const menu = () => within(screen.getByRole("complementary", { name: "Main menu" }));

afterEach(() => vi.unstubAllGlobals());

describe("the Shifts switch", () => {
  it("off: no Roster in the menu, no Shifts tab, and the roster page says why", async () => {
    start("admin", "/settings", false);
    await waitFor(() => expect(menu().queryByRole("link", { name: "Roster" })).not.toBeInTheDocument());
    expect(screen.queryByRole("tab", { name: "Shifts" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "System" })).toBeInTheDocument();
  });

  it("off: the roster page explains instead of showing the grid", async () => {
    start("hr", "/roster", false);
    expect(await screen.findByText("Shifts are switched off")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Roster" })).not.toBeInTheDocument();
  });

  it("on: Roster is in the menu and Settings has the Shifts tab", async () => {
    start("admin", "/settings", true);
    expect(await screen.findByRole("tab", { name: "Shifts" })).toBeInTheDocument();
    expect(menu().getByRole("link", { name: "Roster" })).toBeInTheDocument();
  });

  it("off: attendance offers the calendar-day statuses and no shift columns", async () => {
    start("hr", "/attendance", false);
    const status = await screen.findByLabelText("Status");
    await waitFor(() =>
      expect(
        within(status)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual([
        "All statuses",
        "Complete (in and out)",
        "Missing IN",
        "Missing OUT",
        "Check (passes, but none where they count)",
      ]),
    );
  });

  it("an admin switches shifts off with a reason", async () => {
    const user = userEvent.setup();
    const api = start("admin", "/settings", true);
    await user.click(await screen.findByRole("tab", { name: "System" }));
    await user.click(await screen.findByRole("switch", { name: "Shifts" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Switch shifts off?");
    await user.type(within(dialog).getByLabelText(/Reason/), "pilot without shifts");
    await user.click(within(dialog).getByRole("button", { name: "Switch off" }));
    await waitFor(() => expect(api.find("PUT", "/api/settings/shifts")).toHaveLength(1));
    expect(api.find("PUT", "/api/settings/shifts")[0]!.body).toEqual({
      enabled: false,
      reason: "pilot without shifts",
    });
  });

  it("a viewer or a guard can't reach the switch", async () => {
    for (const role of ["viewer", "guard"]) {
      start(role, "/settings", true);
      expect(await screen.findByRole("heading", { name: "Not for your role" })).toBeInTheDocument();
      document.body.innerHTML = "";
      vi.unstubAllGlobals();
    }
  });
});
