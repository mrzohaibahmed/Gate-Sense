import { createContext, type ReactNode, useCallback, useContext } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";

import { type LiveState, type UnknownMessage, useLiveEvents } from "./useLiveEvents";

const LiveContext = createContext<LiveState>({ status: "closed", events: [], cameras: {}, decisions: {} });

/** One WebSocket for the whole app (inside the logged-in shell). A new unknown visitor shows an alert. */
export function LiveProvider({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) {
  const navigate = useNavigate();
  const onVisitor = useCallback(
    (v: UnknownMessage) => {
      toast.warning(`Unknown visitor at ${v.camera}`, {
        id: `visitor-${v.id}`,
        description: v.closest_name
          ? `Matches nobody enrolled (closest: ${v.closest_name}, ${v.score.toFixed(2)})`
          : "Matches nobody enrolled",
        action: { label: "View", onClick: () => void navigate("/visitors") },
      });
    },
    [navigate],
  );
  const state = useLiveEvents({ enabled, onVisitor });
  return <LiveContext.Provider value={state}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveState {
  return useContext(LiveContext);
}
