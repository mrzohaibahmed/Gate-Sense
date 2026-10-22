import { useState } from "react";
import { Outlet, useLocation } from "react-router";

import { ErrorBoundary } from "@/components/states";
import { TooltipProvider } from "@/components/ui/misc";
import { LiveProvider } from "@/live/LiveProvider";

import { CommandPalette } from "./CommandPalette";
import { FiltersProvider } from "./filters";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

const COLLAPSED_KEY = "gatesense.sidebar.collapsed";

function storedCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

/** The logged-in layout: sidebar, top bar, and the page (each page has its own error boundary). */
export function AppShell() {
  const [collapsed, setCollapsed] = useState(storedCollapsed);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const location = useLocation();
  const toggle = () =>
    setCollapsed((c) => {
      try {
        window.localStorage.setItem(COLLAPSED_KEY, c ? "0" : "1");
      } catch {
        /* not remembered */
      }
      return !c;
    });
  return (
    <TooltipProvider delayDuration={300}>
      <FiltersProvider>
        <LiveProvider>
          <div className="flex min-h-screen">
            <Sidebar
              collapsed={collapsed}
              onToggle={toggle}
              mobileOpen={mobileOpen}
              onNavigate={() => setMobileOpen(false)}
            />
            <div className="flex min-w-0 flex-1 flex-col">
              <TopBar onMenu={() => setMobileOpen(true)} onSearch={() => setSearchOpen(true)} />
              <main className="min-w-0 flex-1">
                <ErrorBoundary key={location.pathname} label="this page">
                  <Outlet />
                </ErrorBoundary>
              </main>
            </div>
          </div>
          <CommandPalette open={searchOpen} onOpenChange={setSearchOpen} />
        </LiveProvider>
      </FiltersProvider>
    </TooltipProvider>
  );
}

/** A page's title row: title on the left, actions on the right; wraps on a phone. */
export function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2 px-4 pt-4 pb-2">
      <h1 className="mr-auto text-lg font-semibold">{title}</h1>
      {children}
    </div>
  );
}
