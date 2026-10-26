import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { NavLink } from "react-router";

import { canAny, useMe } from "@/auth/auth";
import { Tooltip } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

import { NAV } from "./nav";
import { useShiftsEnabled } from "./shiftMode";

/** Left navigation: only the items the role may open. Collapses to icons on a desktop; a drawer on a phone. */
export function Sidebar({
  collapsed,
  onToggle,
  mobileOpen,
  onNavigate,
}: {
  collapsed: boolean;
  onToggle: () => void;
  mobileOpen: boolean;
  onNavigate: () => void;
}) {
  const { data: me } = useMe();
  const shifts = useShiftsEnabled();
  const items = NAV.filter((item) => canAny(me, item.permissions) && (shifts || !item.shifts));
  return (
    <>
      {mobileOpen && <div className="fixed inset-0 z-30 bg-black/40 md:hidden" onClick={onNavigate} aria-hidden />}
      <aside
        aria-label="Main menu"
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-[width,transform] md:sticky md:top-0 md:h-screen md:translate-x-0",
          collapsed ? "md:w-14" : "md:w-56",
          mobileOpen ? "w-56 translate-x-0" : "w-56 -translate-x-full",
        )}
      >
        <div className={cn("flex h-12 items-center gap-2 border-b border-sidebar-border px-4", collapsed && "md:px-2")}>
          <div className="flex size-7 shrink-0 items-center justify-center rounded-md bg-primary text-xs font-bold text-primary-foreground">
            GS
          </div>
          <span className={cn("font-semibold", collapsed && "md:hidden")}>GateSense</span>
        </div>
        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto p-2">
          {items.map((item) => {
            const link = (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                onClick={onNavigate}
                className={({ isActive }) =>
                  cn(
                    "flex h-8 items-center gap-2 rounded-md px-2 text-sm hover:bg-muted",
                    isActive && "bg-accent font-medium text-accent-foreground hover:bg-accent",
                    collapsed && "md:justify-center md:px-0",
                  )
                }
              >
                <item.icon className="size-4 shrink-0" aria-hidden />
                <span className={cn(collapsed && "md:sr-only")}>{item.label}</span>
              </NavLink>
            );
            return collapsed ? (
              <Tooltip key={item.to} content={item.label}>
                {link}
              </Tooltip>
            ) : (
              link
            );
          })}
        </nav>
        <button
          type="button"
          onClick={onToggle}
          className="hidden h-10 items-center gap-2 border-t border-sidebar-border px-4 text-xs text-muted-foreground hover:bg-muted md:flex"
          aria-label={collapsed ? "Expand the menu" : "Collapse the menu"}
        >
          {collapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
          {!collapsed && "Collapse"}
        </button>
      </aside>
    </>
  );
}
