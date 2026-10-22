import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routes } from "@/app/routes";
import { makeMe, PERMISSIONS, renderRoutes } from "@/test/render";

// The shell asks the API for sites and notifications, and opens the live WebSocket: answer with nothing.
class SilentSocket {
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  close() {}
}

const EMPTY_OVERVIEW = {
  date: "2026-10-22",
  people: 0,
  present: 0,
  inside_now: 0,
  left: 0,
  absent: 0,
  missing_exit: 0,
  missing_entry: 0,
  arrivals_by_hour: [],
  cameras: [],
};

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (url: string) =>
        new Response(
          JSON.stringify(
            url.startsWith("/api/overview") ? EMPTY_OVERVIEW : { items: [], total: 0, page: 1, page_size: 50 },
          ),
          { headers: { "content-type": "application/json" } },
        ),
    ),
  );
  vi.stubGlobal("WebSocket", SilentSocket);
});
afterEach(() => vi.unstubAllGlobals());

function menu() {
  return within(screen.getByRole("complementary", { name: "Main menu" }));
}

describe("route guards", () => {
  it("sends a visitor who isn't logged in to the login, remembering the page", async () => {
    const { router } = renderRoutes(routes, "/attendance", null);
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(router.state.location.search).toBe("?next=%2Fattendance");
  });

  it("sends a user who must change the password to that page first", async () => {
    const me = makeMe("admin", PERMISSIONS.admin!, { must_change_password: true });
    const { router } = renderRoutes(routes, "/", me);
    expect(await screen.findByRole("heading", { name: "Change your password" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/change-password");
  });

  it("shows the menu items of the role only", async () => {
    renderRoutes(routes, "/", makeMe("viewer", PERMISSIONS.viewer!));
    await screen.findByRole("complementary", { name: "Main menu" });
    const labels = menu()
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(labels).toEqual(["Overview", "Attendance", "People", "Reports"]);
  });

  it("an admin sees every item", async () => {
    renderRoutes(routes, "/", makeMe("admin", PERMISSIONS.admin!));
    await screen.findByRole("complementary", { name: "Main menu" });
    expect(menu().getAllByRole("link")).toHaveLength(8);
  });

  it("a guard sees live cameras but not people", async () => {
    renderRoutes(routes, "/", makeMe("guard", PERMISSIONS.guard!));
    await screen.findByRole("complementary", { name: "Main menu" });
    expect(menu().getByRole("link", { name: "Live cameras" })).toBeInTheDocument();
    expect(menu().queryByRole("link", { name: "People" })).not.toBeInTheDocument();
  });

  it("shows 403 for a page the role lacks, typed into the address bar", async () => {
    renderRoutes(routes, "/settings", makeMe("viewer", PERMISSIONS.viewer!));
    expect(await screen.findByRole("heading", { name: "Not for your role" })).toBeInTheDocument();
  });

  it("shows 404 for an unknown address", async () => {
    renderRoutes(routes, "/no-such-page", makeMe("hr", PERMISSIONS.hr!));
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("a logged-in user on /login goes on to the page they asked for", async () => {
    const { router } = renderRoutes(routes, "/login?next=%2Fpeople", makeMe("hr", PERMISSIONS.hr!));
    await screen.findByRole("heading", { name: "People" });
    expect(router.state.location.pathname).toBe("/people");
  });
});
