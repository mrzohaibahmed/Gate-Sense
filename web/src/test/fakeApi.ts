import { vi } from "vitest";

type Handler = (url: URL, body: unknown) => unknown;

/**
 * A fake API for fetch: handlers keyed by "METHOD /path" (the path without the query). Unknown calls answer an
 * empty page. Every call is recorded with its parsed URL and JSON body.
 */
export function fakeApi(handlers: Record<string, Handler>) {
  const calls: { method: string; url: URL; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    let body: unknown = undefined;
    if (typeof init?.body === "string") body = JSON.parse(init.body);
    else if (init?.body) body = init.body;
    calls.push({ method, url, body });
    const handler = handlers[`${method} ${url.pathname}`];
    const result = handler ? handler(url, body) : { items: [], total: 0, page: 1, page_size: 50 };
    if (result instanceof Response) return result;
    return new Response(JSON.stringify(result), { status: 200, headers: { "content-type": "application/json" } });
  });
  return {
    fetchMock,
    calls,
    /** Calls to this path (query ignored), e.g. find("GET", "/api/attendance"). */
    find: (method: string, path: string) => calls.filter((c) => c.method === method && c.url.pathname === path),
  };
}

export function apiError(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), { status, headers: { "content-type": "application/json" } });
}

/** A WebSocket that never connects (the shell opens one). */
export class SilentSocket {
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  close() {}
}

export function page<T>(items: T[]) {
  return { items, total: items.length, page: 1, page_size: 100 };
}
