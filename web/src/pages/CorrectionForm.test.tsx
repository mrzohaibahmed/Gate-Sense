import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AttendanceEvent } from "@/api/types";
import { apiError, fakeApi } from "@/test/fakeApi";
import { testQueryClient, withClient } from "@/test/render";

import { CorrectionForm } from "./CorrectionForm";

const EVENTS: AttendanceEvent[] = [
  {
    id: 11,
    timestamp: "2026-10-21T08:05:00",
    original_timestamp: null,
    person_id: 1,
    employee_code: "1001",
    name: "Ayesha",
    camera_id: 1,
    camera: "entrance",
    direction: "IN",
    site_id: 1,
    score: 0.71,
    manual: false,
    voided: false,
    snapshot_url: null,
  },
];

let api: ReturnType<typeof fakeApi>;
let answer: () => unknown;

beforeEach(() => {
  answer = () => ({ ...EVENTS[0], id: 12 });
  api = fakeApi({ "POST /api/attendance/corrections": () => answer() });
  vi.stubGlobal("fetch", api.fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function setup() {
  const client = testQueryClient();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const onDone = vi.fn();
  render(<CorrectionForm personId={1} date="2026-10-21" events={EVENTS} onDone={onDone} />, {
    wrapper: withClient(client),
  });
  return { user: userEvent.setup(), invalidate, onDone };
}

describe("correction form", () => {
  it("needs a time and a reason before anything is sent", async () => {
    const { user } = setup();
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    expect(await screen.findByText("Pick a time")).toBeInTheDocument();
    expect(screen.getByText("A reason is required (it goes to the audit log)")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Time"), "18:00");
    await user.type(screen.getByLabelText("Reason (required)"), "   ");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    expect(await screen.findByText("A reason is required (it goes to the audit log)")).toBeInTheDocument();
    expect(api.calls).toHaveLength(0);
  });

  it("adds a missing OUT", async () => {
    const { user, invalidate, onDone } = setup();
    await user.selectOptions(screen.getByLabelText("Direction"), "OUT");
    await user.type(screen.getByLabelText("Time"), "18:00");
    await user.type(screen.getByLabelText("Reason (required)"), "forgot to look at the exit camera");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.calls[0]?.body).toEqual({
      action: "add",
      person_id: 1,
      direction: "OUT",
      timestamp: "2026-10-21T18:00:00",
      reason: "forgot to look at the exit camera",
    });
    const keys = invalidate.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey));
    expect(keys).toEqual(expect.arrayContaining(['["attendance"]', '["events"]', '["overview"]']));
  });

  it("changes an event's time", async () => {
    const { user, onDone } = setup();
    await user.selectOptions(screen.getByLabelText("Correction"), "change");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    expect(await screen.findByText("Pick the event to change")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Event"), "11");
    await user.type(screen.getByLabelText("New time"), "07:45");
    await user.type(screen.getByLabelText("Reason (required)"), "badge log shows 07:45");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.calls[0]?.body).toEqual({
      action: "change",
      event_id: 11,
      timestamp: "2026-10-21T07:45:00",
      reason: "badge log shows 07:45",
    });
  });

  it("shows the API's refusal", async () => {
    answer = () => apiError(422, "The time is in the future.");
    const { user, onDone } = setup();
    await user.type(screen.getByLabelText("Time"), "23:59");
    await user.type(screen.getByLabelText("Reason (required)"), "x");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The time is in the future.");
    expect(onDone).not.toHaveBeenCalled();
  });
});
