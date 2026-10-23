import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PersonDetail } from "@/api/types";
import { routes } from "@/app/routes";
import { apiError, fakeApi, page, SilentSocket } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

const person: PersonDetail = {
  id: 1,
  employee_code: "1001",
  name: "Ayesha",
  department: "Stores",
  shift: null,
  active: true,
  site_id: 1,
  site: "Main",
  photos: 2,
  photo_url: null,
  embeddings: 2,
  photo_list: [
    {
      file: "a.jpg",
      url: "/api/people/1/photos/a.jpg",
      accepted: true,
      message: null,
      score: 90,
      yaw: 1,
      pitch: 2,
      step: null,
    },
    {
      file: "b.jpg",
      url: "/api/people/1/photos/b.jpg",
      accepted: true,
      message: null,
      score: 80,
      yaw: 3,
      pitch: 1,
      step: null,
    },
  ],
  recent_attendance: [],
  recent_events: [],
};

let api: ReturnType<typeof fakeApi>;

beforeEach(() => {
  api = fakeApi({
    "GET /api/people/1": () => person,
    "PATCH /api/people/1": (_url, body) => ({ ...person, ...(body as object) }),
    "DELETE /api/people/1/photos/a.jpg": () => ({ ...person, photos: 1, embeddings: 1 }),
    "POST /api/people/1/delete-face-data": () => ({ photos: 2, embeddings: 2, deactivated: false }),
    "GET /api/sites": () => page([{ id: 1, name: "Main", timezone: "local" }]),
  });
  vi.stubGlobal("fetch", api.fetchMock);
  vi.stubGlobal("WebSocket", SilentSocket);
});
afterEach(() => vi.unstubAllGlobals());

async function openProfile(role: string) {
  renderRoutes(routes, "/people/1", makeMe(role, PERMISSIONS[role]!));
  await screen.findByRole("heading", { name: "Ayesha" });
}

describe("person profile", () => {
  it("HR edits the name and department; the employee code is read-only", async () => {
    const user = userEvent.setup();
    await openProfile("hr");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Employee code")).toBeDisabled();
    expect(within(dialog).getByLabelText("Employee code")).toHaveValue("1001");
    const name = within(dialog).getByLabelText("Name");
    await user.clear(name);
    await user.type(name, "Ayesha Khan");
    await user.clear(within(dialog).getByLabelText("Department"));
    await user.type(within(dialog).getByLabelText("Department"), "Office");

    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByText(/A reason is required/)).toBeInTheDocument();
    expect(api.find("PATCH", "/api/people/1")).toHaveLength(0);

    await user.type(within(dialog).getByLabelText("Reason (required)"), "name spelling");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.find("PATCH", "/api/people/1")).toHaveLength(1));
    expect(api.find("PATCH", "/api/people/1")[0]!.body).toEqual({
      name: "Ayesha Khan",
      department: "Office",
      shift: null,
      site_id: 1,
      reason: "name spelling",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("an API refusal stays in the dialog", async () => {
    const user = userEvent.setup();
    api = fakeApi({
      "GET /api/people/1": () => person,
      "PATCH /api/people/1": () => apiError(422, "name: too long"),
    });
    vi.stubGlobal("fetch", api.fetchMock);
    await openProfile("admin");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason (required)"), "x");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByText("name: too long")).toBeInTheDocument();
  });

  it("deletes one photo with a reason", async () => {
    const user = userEvent.setup();
    await openProfile("hr");
    await user.click(screen.getByRole("button", { name: "Delete photo a.jpg" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/face data rebuilt/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Reason (required)"), "blurry");
    await user.click(within(dialog).getByRole("button", { name: "Delete photo" }));
    await waitFor(() => expect(api.find("DELETE", "/api/people/1/photos/a.jpg")).toHaveLength(1));
    expect(api.find("DELETE", "/api/people/1/photos/a.jpg")[0]!.url.searchParams.get("reason")).toBe("blurry");
  });

  it("delete face data needs “I understand”, then can also deactivate", async () => {
    const user = userEvent.setup();
    await openProfile("admin");
    await user.click(screen.getByRole("button", { name: "Delete face data" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/attendance history stay/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Reason (required)"), "left the company");
    await user.click(within(dialog).getByRole("button", { name: "Delete face data" }));
    expect(await within(dialog).findByText(/Tick “I understand” first/)).toBeInTheDocument();
    expect(api.find("POST", "/api/people/1/delete-face-data")).toHaveLength(0);

    await user.click(within(dialog).getByLabelText(/Also deactivate/));
    await user.click(within(dialog).getByLabelText(/I understand/));
    await user.click(within(dialog).getByRole("button", { name: "Delete face data" }));
    await waitFor(() => expect(api.find("POST", "/api/people/1/delete-face-data")).toHaveLength(1));
    expect(api.find("POST", "/api/people/1/delete-face-data")[0]!.body).toEqual({
      reason: "left the company",
      deactivate: true,
    });
  });

  it("a viewer can't edit or delete", async () => {
    await openProfile("viewer");
    expect(screen.getByAltText("Photo a.jpg")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete photo a.jpg" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete face data" })).not.toBeInTheDocument();
  });

  it("links to adding photos from files", async () => {
    await openProfile("hr");
    expect(screen.getByRole("link", { name: /Add photos from files/ })).toHaveAttribute(
      "href",
      "/enrollment?person=1&tab=upload",
    );
  });
});
