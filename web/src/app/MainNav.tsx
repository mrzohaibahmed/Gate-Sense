import { Menu } from "lucide-react";
import { useState } from "react";
import { NavLink } from "react-router";

import { canAny, useMe } from "@/auth/auth";
import { Button } from "@/components/ui/button";
import { Dialog, DialogDescription, DialogTitle, SheetContent } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

import { NAV, type NavItem } from "./nav";
import { useShiftsEnabled } from "./shiftMode";
import { useVisitorLog } from "./visitorLog";

/** Only the pages the role may open (and Roster and Unknown visitors only while they are in use). */
function useVisibleNav(): { items: NavItem[]; waiting: number } {
  const { data: me } = useMe();
  const shifts = useShiftsEnabled();
  const visitors = useVisitorLog();
  const items = NAV.filter(
    (item) =>
      canAny(me, item.permissions) &&
      (shifts || !item.shifts) &&
      (visitors.enabled || visitors.waiting > 0 || !item.visitors),
  );
  return { items, waiting: visitors.waiting };
}

function WaitingBadge({ waiting }: { waiting: number }) {
  return (
    <span
      className="rounded-full bg-warn/15 px-1.5 text-[11px] leading-4 font-semibold text-warn tabular-nums"
      aria-label={`${waiting} not reviewed`}
    >
      {waiting > 99 ? "99+" : waiting}
    </span>
  );
}

/** The horizontal menu of a wide screen. */
export function DesktopNav() {
  const { items, waiting } = useVisibleNav();
  return (
    <nav aria-label="Main menu" className="hidden min-w-0 items-center gap-0.5 xl:flex">
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/"}
          className={({ isActive }) =>
            cn(
              "flex h-10 items-center gap-1.5 rounded-lg px-2.5 text-sm whitespace-nowrap text-foreground/80 hover:bg-muted hover:text-foreground",
              isActive && "bg-accent font-medium text-accent-foreground hover:bg-accent hover:text-accent-foreground",
            )
          }
        >
          {item.label}
          {item.visitors && waiting > 0 && <WaitingBadge waiting={waiting} />}
        </NavLink>
      ))}
    </nav>
  );
}

/** The same pages in a drawer, on a screen too narrow for them. */
export function MobileNav() {
  const [open, setOpen] = useState(false);
  const { items, waiting } = useVisibleNav();
  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="size-10 xl:hidden"
        onClick={() => setOpen(true)}
        aria-label="Open the menu"
      >
        <Menu />
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <SheetContent className="right-auto left-0 w-72 border-r border-l-0 sm:max-w-xs">
          <div className="border-b px-4 py-4 pr-10">
            <DialogTitle>GateSense</DialogTitle>
            <DialogDescription className="sr-only">Go to another page</DialogDescription>
          </div>
          <nav aria-label="Pages" className="flex flex-col gap-1 p-2">
            {items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                onClick={() => setOpen(false)}
                className={({ isActive }) =>
                  cn(
                    "flex h-11 items-center gap-3 rounded-lg px-3 hover:bg-muted",
                    isActive && "bg-accent font-medium text-accent-foreground hover:bg-accent",
                  )
                }
              >
                <item.icon className="size-4 shrink-0" aria-hidden />
                {item.label}
                {item.visitors && waiting > 0 && (
                  <span className="ml-auto">
                    <WaitingBadge waiting={waiting} />
                  </span>
                )}
              </NavLink>
            ))}
          </nav>
        </SheetContent>
      </Dialog>
    </>
  );
}
