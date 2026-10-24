import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routes } from "@/app/routes";
import type { Decision } from "@/live/useLiveEvents";
import { fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

import { DecisionsTable } from "./LiveCameras";

const d = (over: Partial<Decision>): Decision => ({
  time: "08:05:01",
  kind: "match",
  track: 3,
  name: "waqas",
  score: 0.71,
  second: 0.42,
  margin: 0.29,
  face_px: 150,
  liveness: 0.97,
  detail: "vote 1/3, live 1/3",
  ...over,
});

describe("DecisionsTable", () => {
  it("lists the newest decision first with score, second, margin, face size and skip reason", () => {
    render(
      <DecisionsTable
        decisions={[
          d({}),
          d({ time: "08:05:02", kind: "unknown", name: null, score: 0.4, second: 0.37, margin: 0.03, detail: "" }),
          d({
            time: "08:05:03",
            kind: "skip",
            track: 4,
            name: null,
            score: null,
            second: null,
            margin: null,
            face_px: 60,
            detail: "too small (60px < 80)",
          }),
        ]}
      />,
    );
    const rows = within(screen.getByRole("table", { name: "Decisions" }))
      .getAllByRole("row")
      .slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[0]!.textContent)).toEqual([
      "08:05:03",
      "08:05:02",
      "08:05:01",
    ]);
    expect(within(rows[0]!).getByText("skip")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("too small (60px < 80)")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("60")).toBeInTheDocument();
    const match = within(rows[2]!);
    expect(match.getByText("0.710")).toBeInTheDocument();
    expect(match.getByText("0.420")).toBeInTheDocument();
    expect(match.getByText("0.290")).toBeInTheDocument();
    expect(match.getByText("150")).toBeInTheDocument();
    expect(match.getByText("0.97")).toBeInTheDocument(); // the frame's live score
    expect(within(rows[1]!).getByText("0.030").className).toContain("text-warn"); // a small margin stands out
  });

  it("shows a spoof as a skip with its live score, in red", () => {
    render(
      <DecisionsTable
        decisions={[
          d({
            kind: "skip",
            name: null,
            score: null,
            second: null,
            margin: null,
            face_px: 140,
            liveness: 0.12,
            detail: "spoof (0.12)",
          }),
          d({ time: "08:05:02", kind: "skip", liveness: null, detail: "blurry (var 20 < 60)" }),
        ]}
      />,
    );
    const [blurry, spoof] = within(screen.getByRole("table", { name: "Decisions" }))
      .getAllByRole("row")
      .slice(1);
    expect(within(spoof!).getByText("skip").className).toContain("text-destructive");
    expect(within(spoof!).getByText("0.12").className).toContain("text-destructive");
    expect(within(spoof!).getByText("spoof (0.12)").className).toContain("text-destructive");
    expect(within(blurry!).getByText("skip").className).not.toContain("text-destructive");
    expect(within(blurry!).getAllByRole("cell")[8]!.textContent).toBe(""); // not checked: no live score
  });

  it("says when nothing was decided yet", () => {
    render(<DecisionsTable decisions={[]} />);
    expect(screen.getByText("No decisions yet")).toBeInTheDocument();
  });
});

describe("decisions tab in the full camera view", () => {
  const cam = {
    id: 1,
    position: 0,
    name: "entrance",
    site_id: 1,
    site: "Main",
    direction: "IN",
    type: "webcam",
    mirror: false,
    source: 0,
    roi: null,
    missing_variables: [],
    worker: { state: "running", camera: "entrance" },
  };

  async function openFull(role: string) {
    const api = fakeApi({ "GET /api/cameras": () => page([cam]) });
    vi.stubGlobal("fetch", api.fetchMock);
    vi.stubGlobal("WebSocket", SilentSocket);
    const user = userEvent.setup();
    renderRoutes(routes, "/live", makeMe(role, PERMISSIONS[role]!));
    await user.click(await screen.findByRole("button", { name: "Open entrance full screen" }));
    return screen.findByRole("dialog");
  }
  afterEach(() => vi.unstubAllGlobals());

  it("is there for an admin", async () => {
    const dialog = await openFull("admin");
    expect(within(dialog).getByRole("tab", { name: "Decisions" })).toBeInTheDocument();
  });

  it("is hidden from HR and guards", async () => {
    for (const role of ["hr", "guard"]) {
      const dialog = await openFull(role);
      expect(within(dialog).queryByRole("tab", { name: "Decisions" })).not.toBeInTheDocument();
      cleanup();
    }
  });
});
