import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router";

import { PageSkeleton } from "@/components/states";
import { ForbiddenPage } from "@/pages/ErrorPages";

import { type Permission, canAny, useMe } from "./auth";

/** Logged in, with a password that needn't be changed; otherwise to /login or /change-password. */
export function RequireAuth() {
  const { data: me, isPending } = useMe();
  const location = useLocation();
  if (isPending) return <PageSkeleton fullScreen />;
  if (!me) {
    const next = location.pathname + location.search;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  if (me.must_change_password) return <Navigate to="/change-password" replace />;
  return <Outlet />;
}

/** Logged in at all (the password-change page itself). */
export function RequireLogin() {
  const { data: me, isPending } = useMe();
  if (isPending) return <PageSkeleton fullScreen />;
  if (!me) return <Navigate to="/login" replace />;
  return <Outlet />;
}

/** The page, or the 403 page if the role has none of `permissions`. */
export function RequirePermission({
  permissions,
  children,
}: {
  permissions: readonly Permission[];
  children: ReactNode;
}) {
  const { data: me } = useMe();
  if (!canAny(me, permissions)) return <ForbiddenPage />;
  return <>{children}</>;
}
