import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Shift } from "@/api/types";
import { routes } from "@/app/routes";
import { fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";
import { today } from "@/lib/utils";

const version = (id: number, effective_from: string | null, start: string, planned = false) => ({
  id,
  effective_from,
  start,
  end: "14:30",
  crosses_midnight: false,
  grace_min: 30,
  in_window_before_min: 120,
  out_window_after_min: 240,
  created: "2026-10-01T09:00:00",
  created_by: "admin",
  planned,
});
const MORNING: Shift = {
  id: 1,
  name: "Morning",
  start: "06:30",
  end: "14:30",
  crosses_midnight: false,
  grace_min: 30,
  in_window_before_min: 120,
  out_window_after_min: 240,
  effective_from: null,
  versions: [version(10, null, "06:30"), version(11, "2099-01-01", "07:00", true)],
  roster_days: 12,
};

function start() {
  const api = fakeApi({
    "GET /api/shifts": () => page([MORNING]),
    "PATCH /api/shifts/1": () => MORNING,
    "DELETE /api/shifts/1/versions/11": () => MORNING,
  });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
  renderRoutes(routes, "/settings", makeMe("hr", PERMISSIONS.hr!));
  return api;
}

afterEach(() => vi.unstubAllGlobals());

describe("settings > shifts", () => {
  it("HR sees the Shifts tab; a rostered shift can't be deleted; a planned change shows", async () => {
    start();
    const row = await screen.findByRole("row", { name: "Morning" });
    expect(row).toHaveTextContent("06:30–14:30");
    expect(row).toHaveTextContent("07:00–14:30 from 2099-01-01");
    expect(within(row).getByRole("button", { name: "Delete Morning" })).toBeDisabled();
  });

  it("a time change applies from the chosen date (default today), with a reason", async () => {
    const user = userEvent.setup();
    const api = start();
    await user.click(await screen.findByRole("button", { name: "Edit Morning" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Times apply from")).toHaveValue(today());
    const startTime = within(dialog).getByLabelText("Start");
    await user.clear(startTime);
    await user.type(startTime, "07:00");
    await user.clear(within(dialog).getByLabelText("Times apply from"));
    await user.type(within(dialog).getByLabelText("Times apply from"), "2026-11-01");
    await user.type(within(dialog).getByLabelText(/Reason/), "winter hours");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.find("PATCH", "/api/shifts/1")).toHaveLength(1));
    expect(api.find("PATCH", "/api/shifts/1")[0]!.body).toEqual({
      reason: "winter hours",
      start: "07:00",
      end: "14:30",
      grace_min: 30,
      in_window_before_min: 120,
      out_window_after_min: 240,
      effective_from: "2026-11-01",
    });
  });

  it("a rename alone sends no times; a planned change can be withdrawn", async () => {
    const user = userEvent.setup();
    const api = start();
    await user.click(await screen.findByRole("button", { name: "Edit Morning" }));
    const dialog = await screen.findByRole("dialog");
    await user.clear(within(dialog).getByLabelText("Name"));
    await user.type(within(dialog).getByLabelText("Name"), "Early");
    await user.type(within(dialog).getByLabelText(/Reason/), "clearer");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.find("PATCH", "/api/shifts/1")).toHaveLength(1));
    expect(api.find("PATCH", "/api/shifts/1")[0]!.body).toEqual({ reason: "clearer", name: "Early" });

    await user.click(screen.getByRole("button", { name: "History of Morning" }));
    await user.click(screen.getByRole("button", { name: "Withdraw the change from 2099-01-01" }));
    const confirm = await screen.findByRole("dialog");
    await user.type(within(confirm).getByLabelText(/Reason/), "plan dropped");
    await user.click(within(confirm).getByRole("button", { name: "Withdraw" }));
    await waitFor(() => expect(api.find("DELETE", "/api/shifts/1/versions/11")).toHaveLength(1));
    expect(api.find("DELETE", "/api/shifts/1/versions/11")[0]!.url.searchParams.get("reason")).toBe("plan dropped");
  });
});
