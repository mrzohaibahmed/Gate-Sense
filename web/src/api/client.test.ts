import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, buildPath, buildQuery, errorMessage, resourceUrl, setUnauthorizedHandler } from "./client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("typed API client", () => {
  const fetchMock = vi.fn();
  const unauthorized = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    setUnauthorizedHandler(unauthorized);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
    unauthorized.mockReset();
  });

  it("sends the cookie and builds path and query", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 3, name: "Ayesha" }));
    const person = await api.get("/api/people/{person_id}", { path: { person_id: 3 } });
    expect(person.name).toBe("Ayesha");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/people/3");
    expect(init.credentials).toBe("include");
    expect(init.method).toBe("GET");

    fetchMock.mockResolvedValue(jsonResponse(200, { items: [], total: 0, page: 1, page_size: 50 }));
    await api.get("/api/attendance", { query: { from: "2026-10-22", status: "missing OUT", site_id: null } });
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/attendance?from=2026-10-22&status=missing+OUT");
  });

  it("sends a JSON body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, { id: 9 }));
    await api.post("/api/attendance/corrections", {
      body: { action: "add", person_id: 1, direction: "IN", timestamp: "2026-10-22T08:00:00", reason: "forgot" },
    });
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(init.body))).toMatchObject({ action: "add", reason: "forgot" });
  });

  it("goes to the login on a 401, except for the login and me calls", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Not logged in." }));
    await expect(api.get("/api/people")).rejects.toMatchObject({ status: 401, message: "Not logged in." });
    expect(unauthorized).toHaveBeenCalledTimes(1);

    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Wrong username or password." }));
    await expect(api.post("/api/auth/login", { body: { username: "a", password: "b" } })).rejects.toBeInstanceOf(
      ApiError,
    );
    await expect(api.get("/api/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(unauthorized).toHaveBeenCalledTimes(1);
  });

  it("turns FastAPI errors into readable messages", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(403, { detail: "Your role (viewer) does not allow this (users:manage)." }),
    );
    await expect(api.get("/api/users")).rejects.toMatchObject({
      status: 403,
      message: expect.stringContaining("viewer"),
    });
    expect(errorMessage(422, { detail: [{ loc: ["body", "reason"], msg: "Field required" }] })).toBe(
      "reason: Field required",
    );
    expect(errorMessage(500, null)).toBe("Request failed (500)");
  });

  it("returns null for 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(
      api.delete("/api/sites/{site_id}", { path: { site_id: 2 }, query: { reason: "x" } }),
    ).resolves.toBeNull();
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/sites/2?reason=x");
  });

  it("builds URLs", () => {
    expect(buildPath("/api/cameras/{camera_id}/stream", { camera_id: 1 })).toBe("/api/cameras/1/stream");
    expect(() => buildPath("/api/people/{person_id}", {})).toThrow("missing path parameter person_id");
    expect(buildQuery({ a: [1, 2], b: undefined, c: "" })).toBe("?a=1&a=2");
    expect(resourceUrl("/api/snapshots/{event_id}", { event_id: 5 })).toBe("/api/snapshots/5");
  });
});
