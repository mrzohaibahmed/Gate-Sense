import { createContext, type ReactNode, useCallback, useContext, useEffect } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";

import { type LiveState, type UnknownMessage, useLiveEvents } from "./useLiveEvents";
import { alertVisitor, installAudioUnlock, stopBeep, visitorText } from "./visitorAlert";

const LiveContext = createContext<LiveState>({ status: "closed", events: [], cameras: {}, decisions: {} });

/** How long the visitor toast stays (ms): longer than the default so a guard looking away still sees it. */
export const VISITOR_TOAST_MS = 20_000;

/**
 * One WebSocket for the whole app (inside the logged-in shell). A new unknown visitor shows a toast, beeps and shows
 * a desktop notification (visitorAlert: each can be switched off in the account menu).
 */
export function LiveProvider({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) {
  const navigate = useNavigate();
  useEffect(() => installAudioUnlock(), []);
  const onVisitor = useCallback(
    (v: UnknownMessage) => {
      const open = () => {
        stopBeep(); // seen
        void navigate("/visitors");
      };
      const { title, body } = visitorText(v);
      toast.warning(title, {
        id: `visitor-${v.id}`,
        description: body,
        duration: VISITOR_TOAST_MS,
        action: { label: "View", onClick: open },
        onDismiss: stopBeep, // closed with its X or swiped away
      });
      alertVisitor(v, open);
    },
    [navigate],
  );
  const state = useLiveEvents({ enabled, onVisitor });
  return <LiveContext.Provider value={state}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveState {
  return useContext(LiveContext);
}
