import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { createMemoryRouter, RouterProvider, type RouteObject } from "react-router";

import type { Me } from "@/api/types";
import { meKey } from "@/auth/auth";

export function makeMe(role: string, permissions: string[], extra: Partial<Me> = {}): Me {
  return {
    id: 1,
    username: role,
    full_name: role[0]!.toUpperCase() + role.slice(1),
    email: null,
    role,
    permissions,
    must_change_password: false,
    session_expires: "2026-10-22T17:00:00",
    ...extra,
  };
}

export const PERMISSIONS: Record<string, string[]> = {
  admin: [
    "account:self",
    "users:manage",
    "cameras:read",
    "cameras:control",
    "cameras:manage",
    "sites:read",
    "sites:manage",
    "settings:manage",
    "live:view",
    "attendance:read",
    "attendance:correct",
    "people:read",
    "people:manage",
    "reports:read",
    "audit:read",
  ],
  hr: [
    "account:self",
    "sites:read",
    "cameras:read",
    "live:view",
    "attendance:read",
    "attendance:correct",
    "people:read",
    "people:manage",
    "reports:read",
    "audit:read",
  ],
  viewer: ["account:self", "sites:read", "cameras:read", "attendance:read", "people:read", "reports:read"],
  guard: ["account:self", "sites:read", "cameras:read", "cameras:control", "live:view", "attendance:read"],
};

export function testQueryClient(me?: Me | null): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  if (me !== undefined) client.setQueryData(meKey, me);
  return client;
}

/** Render routes at `path` with a memory router and a query client that already knows the user. */
export function renderRoutes(routes: RouteObject[], path: string, me?: Me | null, client = testQueryClient(me)) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const result = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...result, router, client };
}

export function withClient(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}
