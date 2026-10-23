import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fakeApi, page } from "@/test/fakeApi";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

import { MAX_PHOTOS, UploadPhotos } from "./UploadPhotos";

const people = page([
  {
    id: 1,
    employee_code: "1001",
    name: "Ayesha",
    department: "Stores",
    shift: null,
    active: true,
    site_id: 1,
    site: "Main",
    photos: 0,
    photo_url: null,
    embeddings: 0,
  },
]);

let api: ReturnType<typeof fakeApi>;

beforeEach(() => {
  api = fakeApi({
    "GET /api/people": () => people,
    "POST /api/people/1/photos": (_url, body) => {
      const photos = (body as { photos: { name: string }[] }).photos;
      return {
        results: photos.map((p, i) =>
          i === 0
            ? { name: p.name, ok: true, message: "", score: 88, yaw: 2, pitch: 1, saved_as: "upload_01.jpg" }
            : {
                name: p.name,
                ok: false,
                message: "Two faces: one person per photo.",
                score: null,
                yaw: null,
                pitch: null,
                saved_as: null,
              },
        ),
        saved: 1,
        embeddings: 1,
      };
    },
  });
  vi.stubGlobal("fetch", api.fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const file = (name: string, type = "image/jpeg") => new File(["x"], name, { type });
const prepare = vi.fn(async (f: File) => {
  if (f.name.startsWith("bad")) throw new Error("Not an image the browser can read");
  return `data:image/jpeg;base64,${btoa(f.name)}`;
});

function renderUpload(preselected: number | null = 1) {
  return renderRoutes(
    [{ path: "/enrollment", element: <UploadPhotos preselected={preselected} prepare={prepare} /> }],
    "/enrollment",
    makeMe("hr", PERMISSIONS.hr!),
  );
}

describe("enrollment from photo files", () => {
  it("checks each photo on the server and shows which were saved and why the others weren't", async () => {
    const user = userEvent.setup();
    renderUpload();
    await screen.findByRole("option", { name: /Ayesha/ });
    await user.upload(screen.getByLabelText("Photo files"), [file("front.jpg"), file("group.jpg")]);
    const picked = await screen.findByRole("list", { name: "Picked photos" });
    await within(picked).findByAltText("front.jpg");
    await within(picked).findByAltText("group.jpg");

    await user.click(screen.getByRole("button", { name: "Check and save 2 photos" }));
    const results = await screen.findByRole("list", { name: "Results" });
    expect(within(results).getByText("Saved as upload_01.jpg")).toBeInTheDocument();
    expect(within(results).getByText("Refused")).toBeInTheDocument();
    expect(within(results).getByText("Two faces: one person per photo.")).toBeInTheDocument();
    expect(screen.getByText(/1 of 2 saved/)).toBeInTheDocument();

    const sent = api.find("POST", "/api/people/1/photos")[0]!.body as {
      photos: { name: string; image: string }[];
    };
    expect(sent).not.toHaveProperty("reason"); // no reason asked: the API audits a default one
    expect(sent.photos.map((p) => p.name)).toEqual(["front.jpg", "group.jpg"]);
    expect(sent.photos[0]!.image).toMatch(/^data:image\/jpeg;base64,/);
  });

  it("takes dropped files, skips unreadable ones and can remove a photo", async () => {
    const user = userEvent.setup();
    renderUpload();
    await screen.findByRole("option", { name: /Ayesha/ });
    fireEvent.drop(screen.getByRole("group", { name: "Drop photos here" }), {
      dataTransfer: { files: [file("one.jpg"), file("bad.jpg"), file("notes.txt", "text/plain")] },
    });
    expect(await screen.findByText("Only image files (JPEG or PNG) can be added.")).toBeInTheDocument();
    expect(await screen.findByText("Not an image the browser can read")).toBeInTheDocument();
    await screen.findByAltText("one.jpg");
    expect(screen.getByRole("button", { name: "Check and save 1 photo" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Remove one.jpg" }));
    expect(screen.queryByAltText("one.jpg")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Check and save/ })).toBeDisabled();
  });

  it(`keeps at most ${MAX_PHOTOS} photos and needs a person`, async () => {
    const user = userEvent.setup();
    renderUpload(null);
    await screen.findByRole("option", { name: /Ayesha/ });
    const many = Array.from({ length: MAX_PHOTOS + 2 }, (_, i) => file(`p${i}.jpg`));
    await user.upload(screen.getByLabelText("Photo files"), many);
    expect(
      await screen.findByText(`At most ${MAX_PHOTOS} photos at a time; the rest were left out.`),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole("img")).toHaveLength(MAX_PHOTOS));
    const save = screen.getByRole("button", { name: `Check and save ${MAX_PHOTOS} photos` });
    expect(save).toBeDisabled(); // no person yet

    expect(screen.queryByLabelText("Reason")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Person"), "1");
    expect(save).toBeEnabled();
  });
});
