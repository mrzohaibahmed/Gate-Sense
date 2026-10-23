import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Camera } from "@/api/types";
import { routes } from "@/app/routes";
import { apiError, fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes, testQueryClient } from "@/test/render";

import { CameraLogView } from "./Settings";

const camera = (id: number, name: string, state = "stopped"): Camera => ({
  id,
  position: id - 1,
  name,
  site_id: 1,
  site: "Main",
  direction: id === 1 ? "IN" : "OUT",
  type: "webcam",
  mirror: false,
  source: 0,
  roi: null,
  index: 0,
  missing_variables: [],
  worker: { state } as Camera["worker"],
});

let cameras: Camera[];
let api: ReturnType<typeof fakeApi>;

beforeEach(() => {
  cameras = [camera(1, "entrance", "running"), camera(2, "exit"), camera(3, "yard")];
  api = fakeApi({
    "GET /api/cameras": () => page(cameras),
    "POST /api/cameras/2/move": () => {
      cameras = [cameras[1]!, cameras[0]!, cameras[2]!];
      return cameras;
    },
    "DELETE /api/cameras/3": () => new Response(null, { status: 204 }),
    "GET /api/cameras/2/log": () => ({
      camera: "exit",
      lines: ["08:00:01 started", "08:00:02 match waqas 0.71"],
      size_bytes: 64,
      updated: "2026-10-22T08:00:02",
    }),
  });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
});
afterEach(() => vi.unstubAllGlobals());

const admin = () => makeMe("admin", PERMISSIONS.admin!);

describe("camera list in Settings", () => {
  it("moves a camera up; the first one can't move up and the last can't move down", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/settings", admin());
    await screen.findByText("yard");
    expect(screen.getByRole("button", { name: "Move entrance up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move yard down" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Move exit up" }));
    await waitFor(() => expect(api.find("POST", "/api/cameras/2/move")).toHaveLength(1));
    expect(api.find("POST", "/api/cameras/2/move")[0]!.body).toEqual({ step: -1 });
    await waitFor(() => expect(screen.getByRole("button", { name: "Move exit up" })).toBeDisabled());
  });

  it("deletes a stopped camera with a reason; a running one can't be deleted", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/settings", admin());
    await screen.findByText("yard");
    expect(screen.getByRole("button", { name: "Delete entrance" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Delete yard" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason (required)"), "camera removed");
    await user.click(within(dialog).getByRole("button", { name: "Delete camera" }));
    await waitFor(() => expect(api.find("DELETE", "/api/cameras/3")).toHaveLength(1));
    expect(api.find("DELETE", "/api/cameras/3")[0]!.url.searchParams.get("reason")).toBe("camera removed");
  });

  it("shows the API's refusal in the delete dialog", async () => {
    const user = userEvent.setup();
    api = fakeApi({
      "GET /api/cameras": () => page(cameras),
      "DELETE /api/cameras/3": () => apiError(409, "the yard camera is running: stop it on the Cameras page first"),
    });
    vi.stubGlobal("fetch", api.fetchMock);
    renderRoutes(routes, "/settings", admin());
    await user.click(await screen.findByRole("button", { name: "Delete yard" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason (required)"), "x");
    await user.click(within(dialog).getByRole("button", { name: "Delete camera" }));
    expect(await within(dialog).findByText(/is running: stop it/)).toBeInTheDocument();
  });

  it("opens a camera's log", async () => {
    const user = userEvent.setup();
    renderRoutes(routes, "/settings", admin());
    await user.click(await screen.findByRole("button", { name: "Log of exit" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/match waqas 0\.71/)).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: /Download the whole log/ })).toHaveAttribute(
      "href",
      "/api/cameras/2/log.txt",
    );
    expect(api.find("GET", "/api/cameras/2/log")[0]!.url.searchParams.get("lines")).toBe("500");
  });
});

describe("CameraLogView", () => {
  it("refreshes until auto-refresh is switched off", async () => {
    const user = userEvent.setup();
    const { Dialog, DialogContent } = await import("@/components/ui/dialog");
    renderRoutes(
      [
        {
          path: "/",
          element: (
            <Dialog open>
              <DialogContent>
                <CameraLogView camera={camera(2, "exit")} refreshMs={30} />
              </DialogContent>
            </Dialog>
          ),
        },
      ],
      "/",
      admin(),
      testQueryClient(admin()),
    );
    await screen.findByText(/started/);
    await waitFor(() => expect(api.find("GET", "/api/cameras/2/log").length).toBeGreaterThanOrEqual(3));
    await user.click(screen.getByRole("switch", { name: "Auto-refresh" }));
    const after = api.find("GET", "/api/cameras/2/log").length;
    await new Promise((r) => setTimeout(r, 120));
    expect(api.find("GET", "/api/cameras/2/log").length).toBeLessThanOrEqual(after + 1);
  });
});
