import { isRouteErrorResponse, Link, useRouteError } from "react-router";

import { Button } from "@/components/ui/button";

function Message({ code, title, children }: { code: string; title: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-2 p-4 text-center">
      <p className="text-sm font-semibold text-primary">{code}</p>
      <h1 className="text-xl font-semibold">{title}</h1>
      <div className="max-w-md text-sm text-muted-foreground">{children}</div>
      <Button asChild variant="outline" className="mt-2">
        <Link to="/">Back to the overview</Link>
      </Button>
    </div>
  );
}

export function ForbiddenPage() {
  return (
    <Message code="403" title="Not for your role">
      Your account's role doesn't include this page. Ask an administrator if you need it.
    </Message>
  );
}

export function NotFoundPage() {
  return (
    <Message code="404" title="Page not found">
      There is no page at this address.
    </Message>
  );
}

/** The router's error element: a page that crashed or failed to load. */
export function RouteErrorPage() {
  const error = useRouteError();
  const text = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : "Unknown error";
  return (
    <Message code="Error" title="Something went wrong">
      {text}. Reload the page; if it keeps happening, tell an administrator.
    </Message>
  );
}
