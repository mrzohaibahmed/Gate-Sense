import { Component, type ErrorInfo, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/** Placeholder blocks while a page loads: a title, a row of tiles and a table. */
export function PageSkeleton({ fullScreen }: { fullScreen?: boolean }) {
  return (
    <div
      role="status"
      aria-label="Loading"
      className={cn("flex flex-col gap-4 p-4", fullScreen && "mx-auto min-h-screen max-w-5xl justify-center")}
    >
      <Skeleton className="h-6 w-48" />
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>
      <Skeleton className="h-64" />
    </div>
  );
}

export function TableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div role="status" aria-label="Loading" className="flex flex-col gap-2 p-4">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-7" />
      ))}
    </div>
  );
}

/** Catches a render error in one part of the page; the rest keeps working. */
export class ErrorBoundary extends Component<{ children: ReactNode; label?: string }, { error: Error | null }> {
  override state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("GateSense UI error", error, info.componentStack);
  }

  override render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" className="m-4 flex flex-col items-start gap-2 rounded-lg border border-destructive/40 p-4">
        <p className="font-medium">Something went wrong{this.props.label ? ` in ${this.props.label}` : ""}.</p>
        <p className="text-sm text-muted-foreground">{this.state.error.message}</p>
        <Button variant="outline" size="sm" onClick={() => this.setState({ error: null })}>
          Try again
        </Button>
      </div>
    );
  }
}
