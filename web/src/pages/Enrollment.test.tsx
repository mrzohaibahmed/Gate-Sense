import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClientProvider } from "@tanstack/react-query";

import { fakeApi, page } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, testQueryClient } from "@/test/render";

import { type CameraAdapter, EnrollmentWizard } from "./Enrollment";

const STEPS = [
  {
    key: "front",
    title: "Front",
    instruction: "Look straight at the camera.",
    yaw: [-10, 10],
    pitch: [-20, 20],
    required: true,
  },
  {
    key: "left",
    title: "Slightly left",
    instruction: "Turn your head slightly to your left.",
    yaw: [15, 35],
    pitch: [-20, 20],
    required: true,
  },
  {
    key: "right",
    title: "Slightly right",
    instruction: "Turn your head slightly to your right.",
    yaw: [-35, -15],
    pitch: [-20, 20],
    required: true,
  },
];
const PERSON = {
  id: 7,
  employee_code: "2001",
  name: "Erum",
  department: "HR",
  shift: null,
  active: true,
  site_id: 1,
  site: "Main",
  photos: 0,
  photo_url: null,
  embeddings: 0,
};

let api: ReturnType<typeof fakeApi>;
let good: boolean;

const fakeCamera = (): CameraAdapter => ({
  start: vi.fn(async () => {}),
  stop: vi.fn(),
  grab: vi.fn(async () => new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: "image/jpeg" })),
});

beforeEach(() => {
  good = false;
  api = fakeApi({
    "GET /api/enrollment/steps": () => STEPS,
    "GET /api/people": () => page([]),
    "POST /api/people": () => PERSON,
    "POST /api/enrollment/check": () => ({
      ok: good,
      state: good ? "good" : "bad",
      message: good ? "Good, hold still" : "Move closer",
      yaw: 0,
      pitch: 0,
      face_px: 150,
      score: 80,
      box: [0.4, 0.3, 0.6, 0.6],
      guide: [0.3, 0.2, 0.7, 0.8],
    }),
    "POST /api/enrollment/review": (_url, body) => ({
      photos: (body as { photos: { step: string }[] }).photos.map((p) => ({
        step: p.step,
        ok: true,
        problem: null,
        warnings: [],
        yaw: 0,
        pitch: 0,
        score: 82,
      })),
      missing: [],
      ready: true,
    }),
    "POST /api/people/7/enroll": () => ({
      employee_code: "2001",
      saved: ["1.jpg", "2.jpg", "3.jpg"],
      accepted: 3,
      embeddings: 3,
    }),
  });
  vi.stubGlobal("fetch", api.fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function renderWizard(camera = fakeCamera()) {
  const client = testQueryClient(makeMe("hr", PERMISSIONS.hr!));
  const router = createMemoryRouter(
    [{ path: "/enrollment", element: <EnrollmentWizard camera={camera} checkEveryMs={20} /> }],
    {
      initialEntries: ["/enrollment"],
    },
  );
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { camera, user: userEvent.setup() };
}

const currentStep = () => screen.getByRole("listitem", { current: "step" });

describe("enrollment wizard", () => {
  it("goes through details, the three guided photos, review and save", async () => {
    const { camera, user } = renderWizard();
    expect(await screen.findByText("Details")).toBeInTheDocument();
    expect(currentStep()).toHaveTextContent("Details");

    // Details: a new person (the reason is filled in by default).
    await user.click(screen.getByRole("button", { name: "Add and continue" }));
    expect(await screen.findByText("Enter the name")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Employee code"), "2001");
    await user.type(screen.getByLabelText("Name"), "Erum");
    await user.click(screen.getByRole("button", { name: "Add and continue" }));
    await waitFor(() => expect(currentStep()).toHaveTextContent("Front"));
    expect(api.find("POST", "/api/people")[0]?.body).toMatchObject({ employee_code: "2001", name: "Erum" });
    expect(camera.start).toHaveBeenCalled();

    // Capture stays off while the check says no...
    expect(await screen.findByRole("status", { name: "" })).toBeDefined();
    await screen.findByText("Move closer");
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
    expect(screen.getByTestId("guide-box")).toHaveAttribute("data-state", "bad");
    const firstCheck = api.find("POST", "/api/enrollment/check")[0]!;
    expect(firstCheck.url.searchParams.get("step")).toBe("front");
    expect(firstCheck.body).toBeInstanceOf(Blob);

    // ...and turns on (green guide) when it says yes.
    act(() => {
      good = true;
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    expect(screen.getByTestId("guide-box")).toHaveAttribute("data-state", "good");
    await user.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(currentStep()).toHaveTextContent("Slightly left"));
    await waitFor(() =>
      expect(api.find("POST", "/api/enrollment/check").at(-1)!.url.searchParams.get("step")).toBe("left"),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(currentStep()).toHaveTextContent("Slightly right"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Capture" }));

    // Review, then save.
    await waitFor(() => expect(currentStep()).toHaveTextContent("Review and save"));
    expect(camera.stop).toHaveBeenCalled();
    await screen.findAllByText(/ok · 82/);
    const reviewed = api.find("POST", "/api/enrollment/review")[0]!;
    expect((reviewed.body as { photos: { step: string; image: string }[] }).photos.map((p) => p.step)).toEqual([
      "front",
      "left",
      "right",
    ]);
    expect((reviewed.body as { photos: { image: string }[] }).photos[0]!.image).toMatch(/^data:image\/jpeg;base64,/);
    await user.click(screen.getByRole("button", { name: "Save enrollment" }));
    expect(await screen.findByText(/Erum is enrolled/)).toBeInTheDocument();
    expect(api.find("POST", "/api/people/7/enroll")[0]?.body).toMatchObject({ reason: "enrolled on the dashboard" });
  });

  it("a retake goes back to that step", async () => {
    good = true;
    const { user } = renderWizard();
    await user.type(await screen.findByLabelText("Employee code"), "2001");
    await user.type(screen.getByLabelText("Name"), "Erum");
    await user.click(screen.getByRole("button", { name: "Add and continue" }));
    for (const next of ["Slightly left", "Slightly right", "Review and save"]) {
      await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
      await user.click(screen.getByRole("button", { name: "Capture" }));
      await waitFor(() => expect(currentStep()).toHaveTextContent(next));
    }
    const retakes = await screen.findAllByRole("button", { name: "Retake" });
    await user.click(retakes[1]!);
    await waitFor(() => expect(currentStep()).toHaveTextContent("Slightly left"));
  });

  it("explains a camera that can't start", async () => {
    const camera = fakeCamera();
    camera.start = vi.fn(async () => {
      throw new Error("The camera is busy: another app (or a camera worker) is using it.");
    });
    const { user } = renderWizard(camera);
    await user.type(await screen.findByLabelText("Employee code"), "2001");
    await user.type(screen.getByLabelText("Name"), "Erum");
    await user.click(screen.getByRole("button", { name: "Add and continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The camera is busy");
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
  });
});
