import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClientProvider } from "@tanstack/react-query";

import { apiError, fakeApi, page } from "@/test/fakeApi";
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
let claims: number;
let renewSeconds: number;

const fakeCamera = (): CameraAdapter => ({
  start: vi.fn(async () => {}),
  stop: vi.fn(),
  grab: vi.fn(async () => new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: "image/jpeg" })),
});

beforeEach(() => {
  good = false;
  claims = 0;
  renewSeconds = 20;
  api = fakeApi({
    "GET /api/enrollment/steps": () => STEPS,
    "POST /api/enrollment/webcam": (_url, body) => ({
      claim_id: (body as { claim_id?: string } | undefined)?.claim_id ?? `claim-${++claims}`,
      expires_seconds: 90,
      renew_seconds: renewSeconds,
    }),
    "DELETE /api/enrollment/webcam/claim-1": () => new Response(null, { status: 204 }),
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

describe("the wizard's hold on the webcam", () => {
  async function toCapture(camera = fakeCamera()) {
    const r = renderWizard(camera);
    await r.user.type(await screen.findByLabelText("Employee code"), "2001");
    await r.user.type(screen.getByLabelText("Name"), "Erum");
    await r.user.click(screen.getByRole("button", { name: "Add and continue" }));
    return r;
  }
  const released = () => api.find("DELETE", "/api/enrollment/webcam/claim-1");

  it("claims the webcam before opening it and releases it when the capture steps close", async () => {
    const { camera, user } = await toCapture();
    await waitFor(() => expect(camera.start).toHaveBeenCalled());
    expect(api.find("POST", "/api/enrollment/webcam")).toHaveLength(1);
    expect(released()).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(released()).toHaveLength(1));
    expect(camera.stop).toHaveBeenCalled();
    expect(released()[0]!.url.pathname).toBe("/api/enrollment/webcam/claim-1");
  });

  it("renews the claim while open, and releases it when the tab closes", async () => {
    renewSeconds = 0.05;
    const { camera } = await toCapture();
    await waitFor(() => expect(camera.start).toHaveBeenCalled());
    await waitFor(() => expect(api.find("POST", "/api/enrollment/webcam").length).toBeGreaterThanOrEqual(3));
    expect(api.find("POST", "/api/enrollment/webcam").at(-1)!.body).toEqual({ claim_id: "claim-1" });
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    await waitFor(() => expect(released()).toHaveLength(1));
  });

  it("doesn't open the webcam while a camera worker uses it", async () => {
    api = fakeApi({
      "GET /api/enrollment/steps": () => STEPS,
      "GET /api/people": () => page([]),
      "POST /api/people": () => PERSON,
      "POST /api/enrollment/webcam": () =>
        apiError(409, "The webcam is in use by the entrance camera worker; stop it on the Live cameras page first."),
    });
    vi.stubGlobal("fetch", api.fetchMock);
    const camera = fakeCamera();
    await toCapture(camera);
    expect(await screen.findByRole("alert")).toHaveTextContent("in use by the entrance camera worker");
    expect(camera.start).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
  });

  it("gives the claim back when the camera can't start", async () => {
    const camera = fakeCamera();
    camera.start = vi.fn(async () => {
      throw new Error("No camera found.");
    });
    await toCapture(camera);
    expect(await screen.findByRole("alert")).toHaveTextContent("No camera found.");
    await waitFor(() => expect(released()).toHaveLength(1));
  });
});

describe("which camera", () => {
  const GATE = {
    key: "camera:3",
    kind: "camera",
    label: "gate",
    camera_id: 3,
    direction: "IN",
    site: "Main",
    running: true,
  };
  const WEBCAM = { key: "webcam", kind: "webcam", label: "This PC's webcam", running: false };
  let last: string | null;

  beforeEach(() => {
    last = "camera:3";
    api = fakeApi({
      "GET /api/enrollment/steps": () => STEPS,
      "GET /api/people": () => page([]),
      "POST /api/people": () => PERSON,
      "GET /api/enrollment/sources": () => ({ sources: [WEBCAM, GATE], last }),
      "POST /api/enrollment/webcam": () => ({ claim_id: "claim-1", expires_seconds: 90, renew_seconds: 20 }),
      "POST /api/enrollment/sessions": () => ({
        session_id: "s1",
        camera_id: 3,
        camera: "gate",
        feed: "worker",
        expires_seconds: 90,
        renew_seconds: 20,
      }),
      "POST /api/enrollment/sessions/s1/check": () => ({
        ok: good,
        state: good ? "good" : "bad",
        message: good ? "Good, hold still" : "Move closer",
        yaw: 0,
        pitch: 0,
        face_px: 150,
        score: 80,
        box: [0.4, 0.3, 0.6, 0.6],
        guide: [0.3, 0.2, 0.7, 0.8],
        connected: true,
        width: 1280,
        height: 720,
        feed: "worker",
      }),
      "POST /api/enrollment/sessions/s1/capture": (url) => ({
        step: url.searchParams.get("step"),
        image: `data:image/jpeg;base64,${btoa(url.searchParams.get("step") ?? "")}`,
        width: 1280,
        height: 720,
      }),
      "DELETE /api/enrollment/sessions/s1": () => new Response(null, { status: 204 }),
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
      "POST /api/people/7/enroll": () => ({ employee_code: "2001", saved: ["1.jpg"], accepted: 3, embeddings: 3 }),
    });
    vi.stubGlobal("fetch", api.fetchMock);
  });

  async function toChoice(camera = fakeCamera()) {
    const r = renderWizard(camera);
    await r.user.type(await screen.findByLabelText("Employee code"), "2001");
    await r.user.type(screen.getByLabelText("Name"), "Erum");
    await r.user.click(screen.getByRole("button", { name: "Add and continue" }));
    await screen.findByRole("heading", { name: "Which camera?" });
    return r;
  }

  it("preselects the last choice and takes the photos from the IP camera through the server", async () => {
    good = true;
    const { camera, user } = await toChoice();
    expect(currentStep()).toHaveTextContent("Camera");
    expect(screen.getByRole("radio", { name: /gate/ })).toBeChecked();
    expect(screen.getByText(/recording attendance: its frames are shared/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next: photos" }));

    const preview = await screen.findByRole("img", { name: "Camera preview" });
    expect(preview).toHaveAttribute("src", "/api/enrollment/sessions/s1/stream");
    expect(api.find("POST", "/api/enrollment/sessions")[0]!.body).toEqual({ camera_id: 3 });
    expect(camera.start).not.toHaveBeenCalled(); // the webcam isn't used
    expect(await screen.findByText(/Shared with the running gate camera .* photo 1280×720/)).toBeInTheDocument();
    for (const next of ["Slightly left", "Slightly right", "Review and save"]) {
      await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
      await user.click(screen.getByRole("button", { name: "Capture" }));
      await waitFor(() => expect(currentStep()).toHaveTextContent(next));
    }
    const steps = api.find("POST", "/api/enrollment/sessions/s1/capture").map((c) => c.url.searchParams.get("step"));
    expect(steps).toEqual(["front", "left", "right"]);
    await waitFor(() => expect(api.find("DELETE", "/api/enrollment/sessions/s1")).toHaveLength(1)); // released
    expect(screen.getByRole("img", { name: "Front photo" }).className).not.toContain("-scale-x-100"); // not mirrored
    await user.click(await screen.findByRole("button", { name: "Save enrollment" }));
    expect(await screen.findByText(/Erum is enrolled/)).toBeInTheDocument();
    const saved = api.find("POST", "/api/people/7/enroll")[0]!.body as { source: string; photos: { image: string }[] };
    expect(saved.source).toBe("gate");
    expect(saved.photos[0]!.image).toBe(`data:image/jpeg;base64,${btoa("front")}`); // the server's full-resolution crop
  }, 15_000);

  it("Back returns to the choice and closes the session; the webcam can be chosen instead", async () => {
    const { camera, user } = await toChoice();
    await user.click(screen.getByRole("button", { name: "Next: photos" }));
    await screen.findByRole("img", { name: "Camera preview" });
    await user.click(screen.getByRole("button", { name: "Back" }));
    await screen.findByRole("heading", { name: "Which camera?" });
    await waitFor(() => expect(api.find("DELETE", "/api/enrollment/sessions/s1")).toHaveLength(1));
    await user.click(screen.getByRole("radio", { name: /webcam/ }));
    await user.click(screen.getByRole("button", { name: "Next: photos" }));
    await waitFor(() => expect(camera.start).toHaveBeenCalled());
    expect(api.find("POST", "/api/enrollment/webcam")).toHaveLength(1);
  });

  it("starts on the webcam when that was the last choice", async () => {
    last = "webcam";
    await toChoice();
    expect(screen.getByRole("radio", { name: /webcam/ })).toBeChecked();
  });

  it("explains a camera that is busy", async () => {
    api = fakeApi({
      "GET /api/enrollment/steps": () => STEPS,
      "GET /api/people": () => page([]),
      "POST /api/people": () => PERSON,
      "GET /api/enrollment/sources": () => ({ sources: [WEBCAM, GATE], last: null }),
      "POST /api/enrollment/sessions": () =>
        apiError(409, "The gate camera is in use by the enrollment wizard (admin); wait until they finish."),
    });
    vi.stubGlobal("fetch", api.fetchMock);
    const { user } = await toChoice();
    await user.click(screen.getByRole("radio", { name: /gate/ }));
    await user.click(screen.getByRole("button", { name: "Next: photos" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("in use by the enrollment wizard (admin)");
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
  });

  it("doesn't ask when there are no IP cameras", async () => {
    api = fakeApi({
      "GET /api/enrollment/steps": () => STEPS,
      "GET /api/people": () => page([]),
      "POST /api/people": () => PERSON,
      "GET /api/enrollment/sources": () => ({ sources: [WEBCAM], last: null }),
      "POST /api/enrollment/webcam": () => ({ claim_id: "claim-1", expires_seconds: 90, renew_seconds: 20 }),
    });
    vi.stubGlobal("fetch", api.fetchMock);
    const camera = fakeCamera();
    const { user } = renderWizard(camera);
    await user.type(await screen.findByLabelText("Employee code"), "2001");
    await user.type(screen.getByLabelText("Name"), "Erum");
    await waitFor(() => expect(api.find("GET", "/api/enrollment/sources")).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Add and continue" }));
    await waitFor(() => expect(camera.start).toHaveBeenCalled());
    expect(screen.queryByRole("heading", { name: "Which camera?" })).not.toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Steps" })).not.toHaveTextContent("Camera");
  });
});
