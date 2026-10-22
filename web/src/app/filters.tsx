import { createContext, type ReactNode, useCallback, useContext, useMemo, useState } from "react";

import { today } from "@/lib/utils";

const SITE_KEY = "gatesense.site";

type Filters = {
  /** null = all sites */
  siteId: number | null;
  setSiteId: (id: number | null) => void;
  /** YYYY-MM-DD, the day the screens show (default today) */
  date: string;
  setDate: (date: string) => void;
};

const FiltersContext = createContext<Filters | null>(null);

function storedSite(): number | null {
  try {
    const v = window.localStorage.getItem(SITE_KEY);
    return v ? Number(v) : null;
  } catch {
    return null;
  }
}

/** The top bar's site and date, shared by every screen. The site is remembered on this browser. */
export function FiltersProvider({ children }: { children: ReactNode }) {
  const [siteId, setSite] = useState<number | null>(storedSite);
  const [date, setDate] = useState<string>(today);
  const setSiteId = useCallback((id: number | null) => {
    setSite(id);
    try {
      if (id === null) window.localStorage.removeItem(SITE_KEY);
      else window.localStorage.setItem(SITE_KEY, String(id));
    } catch {
      /* private window: not remembered */
    }
  }, []);
  const value = useMemo(() => ({ siteId, setSiteId, date, setDate }), [siteId, setSiteId, date]);
  return <FiltersContext.Provider value={value}>{children}</FiltersContext.Provider>;
}

export function useFilters(): Filters {
  const ctx = useContext(FiltersContext);
  if (!ctx) throw new Error("useFilters outside FiltersProvider");
  return ctx;
}
