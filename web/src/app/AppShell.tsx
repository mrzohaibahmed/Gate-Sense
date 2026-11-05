import { useState } from "react";
import { Outlet, useLocation } from "react-router";

import { ErrorBoundary } from "@/components/states";
import { TooltipProvider } from "@/components/ui/misc";
import { LiveProvider } from "@/live/LiveProvider";

import { CommandPalette } from "./CommandPalette";
import { FiltersProvider } from "./filters";
import { DatePicker, SiteSwitcher } from "./PageFilters";
import { TopBar } from "./TopBar";

/** The logged-in layout: the header, and the page (each page has its own error boundary). */
export function AppShell() {
  const [searchOpen, setSearchOpen] = useState(false);
  const location = useLocation();
  return (
    <TooltipProvider delayDuration={300}>
      <FiltersProvider>
        <LiveProvider>
          <div className="flex min-h-screen min-w-0 flex-col">
            <TopBar onSearch={() => setSearchOpen(true)} />
            <main className="min-w-0 flex-1">
              <ErrorBoundary key={location.pathname} label="this page">
                <Outlet />
              </ErrorBoundary>
            </main>
          </div>
          <CommandPalette open={searchOpen} onOpenChange={setSearchOpen} />
        </LiveProvider>
      </FiltersProvider>
    </TooltipProvider>
  );
}

/** A page's title row: title on the left, the shared filters it uses, actions on the right; wraps on a phone. */
export function PageHeader({
  title,
  filters,
  children,
}: {
  title: string;
  filters?: "site" | "site and date";
  children?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 px-4 pt-6 pb-2 lg:px-6">
      <h1 className="mr-auto text-2xl font-bold tracking-tight">{title}</h1>
      {filters && <SiteSwitcher />}
      {filters === "site and date" && <DatePicker />}
      {children}
    </div>
  );
}
