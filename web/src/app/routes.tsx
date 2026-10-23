import type { RouteObject } from "react-router";

import { RequireAuth, RequireLogin, RequirePermission } from "@/auth/guards";
import { AttendancePage } from "@/pages/Attendance";
import { AuditPage } from "@/pages/Audit";
import { EnrollmentPage } from "@/pages/Enrollment";
import { ForbiddenPage, NotFoundPage, RouteErrorPage } from "@/pages/ErrorPages";
import { LiveCamerasPage } from "@/pages/LiveCameras";
import { ChangePasswordPage, LoginPage } from "@/pages/Login";
import { OverviewPage } from "@/pages/Overview";
import { PeoplePage, PersonProfilePage } from "@/pages/People";
import { ReportsPage } from "@/pages/Reports";
import { SettingsPage } from "@/pages/Settings";

import { AppShell } from "./AppShell";
import { NAV } from "./nav";

const pages: Record<string, React.ReactNode> = {
  "/": <OverviewPage />,
  "/live": <LiveCamerasPage />,
  "/attendance": <AttendancePage />,
  "/people": <PeoplePage />,
  "/enrollment": <EnrollmentPage />,
  "/reports": <ReportsPage />,
  "/audit": <AuditPage />,
  "/settings": <SettingsPage />,
};

/** Every route; the app and the tests (with a memory router) use the same list. */
export const routes: RouteObject[] = [
  { path: "/login", element: <LoginPage />, errorElement: <RouteErrorPage /> },
  {
    element: <RequireLogin />,
    errorElement: <RouteErrorPage />,
    children: [{ path: "/change-password", element: <ChangePasswordPage /> }],
  },
  {
    element: <RequireAuth />,
    errorElement: <RouteErrorPage />,
    children: [
      {
        element: <AppShell />,
        children: [
          ...NAV.map((item) => ({
            path: item.to,
            element: <RequirePermission permissions={item.permissions}>{pages[item.to]}</RequirePermission>,
          })),
          {
            path: "/people/:id",
            element: (
              <RequirePermission permissions={["people:read"]}>
                <PersonProfilePage />
              </RequirePermission>
            ),
          },
          { path: "/forbidden", element: <ForbiddenPage /> },
          { path: "*", element: <NotFoundPage /> },
        ],
      },
    ],
  },
];
