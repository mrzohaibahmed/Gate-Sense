import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fakeApi, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes, testQueryClient } from "@/test/render";

import { HealthBanner } from "./Overview";

const health = (problems: string[]) => ({
  status: problems.length ? "degraded" : "ok",
  version: "0.1.0",
  started: "2026-10-24T08:00:00",
  uptime_seconds: 3600,
  database: { status: "ok", backend: "sqlite", detail: null },
  cameras: [],
  disk: { path: "D:\\GateSense\\data", free_bytes: 1e9, total_bytes: 1e11, problem: null },
  backup: { last_run: null, ok: null, error: null, problem: null },
  problems,
});

function show(problems: string[]) {
  const api = fakeApi({ "GET /api/health": () => health(problems) });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
  const me = makeMe("guard", PERMISSIONS.guard!);
  renderRoutes([{ path: "/", element: <HealthBanner /> }], "/", me, testQueryClient(me));
  return api;
}

afterEach(() => vi.unstubAllGlobals());

describe("HealthBanner", () => {
  it("lists what needs a look", async () => {
    show(["entrance is stopped (Auto is on: it is being restarted)", "only 1.4 GB free on the data disk"]);
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("These need a look");
    expect(banner).toHaveTextContent("entrance is stopped (Auto is on: it is being restarted)");
    expect(banner).toHaveTextContent("only 1.4 GB free on the data disk");
  });

  it("shows nothing when all is well", async () => {
    const api = show([]);
    await waitFor(() => expect(api.find("GET", "/api/health")).toHaveLength(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
