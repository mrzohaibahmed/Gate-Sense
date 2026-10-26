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

type Tone = "ok" | "warn" | "missing" | "absent" | "error" | "neutral" | "accent";

const STATUS: Record<Status, { tone: Tone; label: string }> = {
  "on time": { tone: "ok", label: "On time" },
  late: { tone: "warn", label: "Late" },
  "early leave": { tone: "warn", label: "Early leave" },
  "missing IN": { tone: "missing", label: "Missing IN" },
  "missing OUT": { tone: "missing", label: "Missing OUT" },
  absent: { tone: "absent", label: "Absent" },
  "not in yet": { tone: "neutral", label: "Not in yet" },
  check: { tone: "error", label: "Check" },
  "off day": { tone: "neutral", label: "Off" },
  unscheduled: { tone: "accent", label: "Unscheduled" },
};

/**
 * A roster day's status: on time, late, early leave, missing IN/OUT, absent, not in yet (the IN window is still
 * open), check (passes, but none where they count), off (OFF on the roster), unscheduled (passes, no roster entry).
 * Pass inside when the day is still running and its last pass is an IN: it shows as a second badge.
 */
export function StatusBadge({ status, inside }: { status: Status; inside?: boolean }) {
  const s = STATUS[status];
  return (
    <span className="inline-flex items-center gap-1">
      <Badge tone={s.tone}>{s.label}</Badge>
      {inside && <Badge tone="inside">Inside</Badge>}
    </span>
  );
}

export function statusLabel(status: Status): string {
  return STATUS[status].label;
}

/** Late / early leave / overtime minutes of a day as short badges (nothing when all are 0 or unknown). */
export function MinutesBadges({
  late,
  early,
  overtime,
}: {
  late?: number | null;
  early?: number | null;
  overtime?: number | null;
}) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {!!late && <Badge tone="warn">late {late} min</Badge>}
      {!!early && <Badge tone="warn">early {early} min</Badge>}
      {!!overtime && <Badge tone="accent">+{overtime} min</Badge>}
    </span>
  );
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
