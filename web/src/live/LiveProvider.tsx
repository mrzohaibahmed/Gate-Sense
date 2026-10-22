import { createContext, type ReactNode, useContext } from "react";

import { type LiveState, useLiveEvents } from "./useLiveEvents";

const LiveContext = createContext<LiveState>({ status: "closed", events: [], cameras: {} });

/** One WebSocket for the whole app (inside the logged-in shell). */
export function LiveProvider({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) {
  const state = useLiveEvents({ enabled });
  return <LiveContext.Provider value={state}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveState {
  return useContext(LiveContext);
}
