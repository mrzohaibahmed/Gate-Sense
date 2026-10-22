import { ArrowDownToLine, ArrowUpFromLine } from "lucide-react";

import type { Direction, Status } from "@/api/types";
import { Badge } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/** IN = blue, OUT = orange, everywhere. */
export function DirectionBadge({ direction, className }: { direction: Direction | string; className?: string }) {
  const isIn = direction === "IN";
  return (
    <Badge tone={isIn ? "in" : "out"} className={className}>
      {isIn ? <ArrowDownToLine className="size-3" aria-hidden /> : <ArrowUpFromLine className="size-3" aria-hidden />}
      {direction}
    </Badge>
  );
}

const STATUS: Record<Status, { tone: "inside" | "left" | "missing" | "absent"; label: string }> = {
  present: { tone: "left", label: "Left" },
  "missing exit": { tone: "missing", label: "Missing exit" },
  "missing entry": { tone: "missing", label: "Missing entry" },
  absent: { tone: "absent", label: "Absent" },
};

/**
 * A day's status. "present" (came in and went out) reads "Left"; for today, "missing exit" means still inside,
 * so pass `today` to show "Inside".
 */
export function StatusBadge({ status, today }: { status: Status; today?: boolean }) {
  if (today && status === "missing exit") return <Badge tone="inside">Inside</Badge>;
  const s = STATUS[status];
  return <Badge tone={s.tone}>{s.label}</Badge>;
}

export function statusLabel(status: Status, today?: boolean): string {
  return today && status === "missing exit" ? "Inside" : STATUS[status].label;
}

const CAMERA_STATE: Record<string, string> = {
  running: "bg-ok",
  starting: "bg-warn animate-pulse",
  "not responding": "bg-destructive",
  stopped: "bg-muted-foreground/40",
};

export function CameraStateDot({ state, className }: { state: string; className?: string }) {
  return (
    <span
      role="img"
      aria-label={state}
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        CAMERA_STATE[state] ?? "bg-muted-foreground/40",
        className,
      )}
    />
  );
}
