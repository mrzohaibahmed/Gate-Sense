import {
  BarChart3,
  CalendarDays,
  ClipboardList,
  LayoutDashboard,
  type LucideIcon,
  ScanFace,
  ScrollText,
  Settings,
  Users,
  Video,
} from "lucide-react";

import type { Permission } from "@/auth/auth";

export type NavItem = {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Shown when the role has any of these (the API enforces the same). */
  permissions: readonly Permission[];
  /** Only while shifts are on (Settings > System > Shifts). */
  shifts?: boolean;
};

export const SETTINGS_PERMISSIONS = [
  "settings:manage",
  "users:manage",
  "cameras:manage",
  "sites:manage",
  "shifts:manage",
] as const;

export const NAV: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard, permissions: ["attendance:read"] },
  { to: "/live", label: "Live cameras", icon: Video, permissions: ["live:view"] },
  { to: "/attendance", label: "Attendance", icon: ClipboardList, permissions: ["attendance:read"] },
  { to: "/roster", label: "Roster", icon: CalendarDays, permissions: ["roster:read"], shifts: true },
  { to: "/people", label: "People", icon: Users, permissions: ["people:read"] },
  { to: "/enrollment", label: "Enrollment", icon: ScanFace, permissions: ["people:manage"] },
  { to: "/reports", label: "Reports", icon: BarChart3, permissions: ["reports:read"] },
  { to: "/audit", label: "Audit log", icon: ScrollText, permissions: ["audit:read"] },
  { to: "/settings", label: "Settings", icon: Settings, permissions: SETTINGS_PERMISSIONS },
];
