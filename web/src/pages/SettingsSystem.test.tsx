import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routes } from "@/app/routes";
import { fakeApi, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

const FALLBACK = "the NVIDIA GPU can't be used (no NVIDIA GPU driver found); using the CPU";

function system(device: object, cameraDevices: object[]) {
  return {
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
    cameras_running: cameraDevices.length,
    device,
    camera_devices: cameraDevices,
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
}

async function openSystem(info: object) {
  vi.stubGlobal(
    "fetch",
    fakeApi({
      "GET /api/settings/shifts": () => ({ enabled: false, updated: null, updated_by: null }),
      "GET /api/settings": () => ({ settings: [] }),
      "GET /api/system": () => info,
    }).fetchMock,
  );
  vi.stubGlobal("WebSocket", SilentSocket);
  renderRoutes(routes, "/settings", makeMe("admin", PERMISSIONS.admin!));
  await userEvent.setup().click(await screen.findByRole("tab", { name: "System" }));
}

afterEach(() => vi.unstubAllGlobals());

describe("Settings > System: the processing device", () => {
  it("shows the GPU the models run on", async () => {
    await openSystem(
      system(
        {
          active: "cuda",
          label: "NVIDIA GPU (CUDA)",
          requested: "auto",
          providers: ["CUDAExecutionProvider", "CPUExecutionProvider"],
          warning: null,
        },
        [{ camera: "entrance", label: "NVIDIA GPU (CUDA)", warning: null }],
      ),
    );
    expect(await screen.findByText("Processing device")).toBeInTheDocument();
    expect(screen.getByText("(GATESENSE_DEVICE=auto)")).toBeInTheDocument();
    expect(screen.getByText("Camera entrance")).toBeInTheDocument();
    expect(screen.getAllByText("NVIDIA GPU (CUDA)", { exact: false })).toHaveLength(2);
    expect(screen.queryByText(FALLBACK)).not.toBeInTheDocument();
  });

  it("shows why it fell back to the CPU, for the app and for each camera", async () => {
    await openSystem(
      system(
        { active: "cpu", label: "CPU", requested: "cuda", providers: ["CPUExecutionProvider"], warning: FALLBACK },
        [{ camera: "exit", label: "CPU", warning: FALLBACK }],
      ),
    );
    expect(await screen.findByText("(GATESENSE_DEVICE=cuda)")).toBeInTheDocument();
    expect(screen.getByText("Camera exit")).toBeInTheDocument();
    expect(screen.getAllByText(FALLBACK)).toHaveLength(2);
  });
});
