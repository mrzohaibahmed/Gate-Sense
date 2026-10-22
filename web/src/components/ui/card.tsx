import * as React from "react";

import { cn } from "@/lib/utils";

/** Cards: 1 px border, no heavy shadow. */
export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border bg-card text-card-foreground", className)} {...props} />;
}

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex items-center justify-between gap-2 border-b px-4 py-2", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-sm font-semibold", className)} {...props} />;
}

export function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4", className)} {...props} />;
}

export function Badge({
  className,
  tone = "neutral",
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & {
  tone?: "neutral" | "in" | "out" | "inside" | "left" | "missing" | "absent" | "ok" | "warn" | "error" | "accent";
}) {
  const tones: Record<string, string> = {
    neutral: "bg-muted text-muted-foreground",
    in: "bg-in/10 text-in",
    out: "bg-out/10 text-out",
    inside: "bg-status-inside/10 text-status-inside",
    left: "bg-status-left/10 text-status-left",
    missing: "bg-status-missing/10 text-status-missing",
    absent: "bg-status-absent/10 text-status-absent",
    ok: "bg-ok/10 text-ok",
    warn: "bg-warn/10 text-warn",
    error: "bg-destructive/10 text-destructive",
    accent: "bg-accent text-accent-foreground",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap",
        tones[tone],
        className,
      )}
      {...props}
    />
  );
}

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div aria-hidden className={cn("animate-pulse rounded-md bg-muted", className)} {...props} />;
}

export function Separator({ className, vertical }: { className?: string; vertical?: boolean }) {
  return (
    <div
      role="separator"
      aria-orientation={vertical ? "vertical" : "horizontal"}
      className={cn("shrink-0 bg-border", vertical ? "h-full w-px" : "h-px w-full", className)}
    />
  );
}
