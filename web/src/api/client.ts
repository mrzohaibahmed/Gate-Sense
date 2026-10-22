/**
 * A small typed fetch client for the GateSense API.
 *
 * Types come from the OpenAPI schema (src/api/schema.d.ts, `npm run gen:api`): the path, its parameters, the JSON
 * body and the success response are all checked. Every request sends the session cookie; a 401 (not logged in, or
 * the session ended) calls the unauthorized handler, which by default goes to /login and comes back afterwards.
 */
import type { paths } from "./schema";

type Method = "get" | "post" | "put" | "patch" | "delete";

/** Paths that have this method. */
export type PathsWith<M extends Method> = {
  [P in keyof paths]: paths[P] extends Record<M, infer O> ? (O extends { responses: unknown } ? P : never) : never;
}[keyof paths];

type Operation<P extends keyof paths, M extends Method> = paths[P] extends Record<M, infer O> ? O : never;
type Parameters_<O> = O extends { parameters: infer X } ? X : never;
type QueryOf<O> = Parameters_<O> extends { query?: infer Q } ? (Q extends undefined ? never : Q) : never;
type PathOf<O> = Parameters_<O> extends { path: infer X } ? (X extends undefined ? never : X) : never;
type BodyOf<O> = O extends { requestBody?: { content: { "application/json": infer B } } } ? B : never;
type Json<R> = R extends { content: { "application/json": infer J } } ? J : null;
type SuccessOf<O> = O extends { responses: infer R }
  ? 200 extends keyof R
    ? Json<R[200]>
    : 201 extends keyof R
      ? Json<R[201]>
      : null
  : never;

export type RequestOptions<O> = {
  query?: QueryOf<O>;
  path?: PathOf<O>;
  body?: BodyOf<O>;
  signal?: AbortSignal;
};

/** The JSON a successful call returns, e.g. ResponseOf<"/api/people", "get">. */
export type ResponseOf<P extends PathsWith<M>, M extends Method> = SuccessOf<Operation<P, M>>;
/** The JSON body a call takes, e.g. BodyFor<"/api/people", "post">. */
export type BodyFor<P extends PathsWith<M>, M extends Method> = BodyOf<Operation<P, M>>;
/** The query parameters a call takes. */
export type QueryFor<P extends PathsWith<M>, M extends Method> = QueryOf<Operation<P, M>>;

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** Where a 401 sends the user. The app sets a router-aware handler; this default reloads into /login. */
let unauthorizedHandler: () => void = () => {
  const next = window.location.pathname + window.location.search;
  if (!window.location.pathname.startsWith("/login")) {
    window.location.assign(`/login?next=${encodeURIComponent(next)}`);
  }
};

export function setUnauthorizedHandler(handler: () => void): void {
  unauthorizedHandler = handler;
}

/** Calls whose 401 is an answer, not a reason to leave the page (the login form, the "who am I" check). */
const NO_REDIRECT = new Set<string>(["/api/auth/login", "/api/auth/me"]);

/** "/api/people/{person_id}" + {person_id: 3} -> "/api/people/3" */
export function buildPath(template: string, params?: Record<string, unknown>): string {
  return template.replace(/\{(\w+)\}/g, (_, key: string) => {
    const value = params?.[key];
    if (value === undefined || value === null) throw new Error(`missing path parameter ${key} for ${template}`);
    return encodeURIComponent(String(value));
  });
}

export function buildQuery(query?: Record<string, unknown>): string {
  if (!query) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    for (const item of Array.isArray(value) ? value : [value]) search.append(key, String(item));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** A readable message from FastAPI's error body ({detail: "..."} or a validation list). */
export function errorMessage(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => {
        const field = (d.loc ?? []).filter((x) => x !== "body" && x !== "query").join(".");
        return field ? `${field}: ${d.msg ?? "invalid"}` : (d.msg ?? "invalid");
      })
      .join("; ");
  }
  return `Request failed (${status})`;
}

async function request<P extends PathsWith<M>, M extends Method>(
  method: M,
  path: P,
  options: RequestOptions<Operation<P, M>> = {},
): Promise<ResponseOf<P, M>> {
  const url =
    buildPath(path as string, options.path as Record<string, unknown> | undefined) +
    buildQuery(options.query as Record<string, unknown> | undefined);
  const hasBody = options.body !== undefined;
  const response = await fetch(url, {
    method: method.toUpperCase(),
    credentials: "include",
    headers: hasBody
      ? { "Content-Type": "application/json", Accept: "application/json" }
      : { Accept: "application/json" },
    body: hasBody ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });
  if (response.status === 401 && !NO_REDIRECT.has(path as string)) unauthorizedHandler();
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    throw new ApiError(response.status, errorMessage(response.status, body), body);
  }
  if (response.status === 204) return null as ResponseOf<P, M>;
  const type = response.headers.get("content-type") ?? "";
  return (type.includes("application/json") ? await response.json() : null) as ResponseOf<P, M>;
}

export const api = {
  get: <P extends PathsWith<"get">>(path: P, options?: RequestOptions<Operation<P, "get">>) =>
    request("get", path, options),
  post: <P extends PathsWith<"post">>(path: P, options?: RequestOptions<Operation<P, "post">>) =>
    request("post", path, options),
  patch: <P extends PathsWith<"patch">>(path: P, options?: RequestOptions<Operation<P, "patch">>) =>
    request("patch", path, options),
  delete: <P extends PathsWith<"delete">>(path: P, options?: RequestOptions<Operation<P, "delete">>) =>
    request("delete", path, options),
};

/**
 * POST a binary body (a camera frame as image/jpeg) to a JSON endpoint, with the same error and 401 handling.
 * Only /api/enrollment/check takes one.
 */
export async function postImage<P extends PathsWith<"post">>(
  path: P,
  image: Blob,
  query?: QueryOf<Operation<P, "post">>,
  signal?: AbortSignal,
): Promise<ResponseOf<P, "post">> {
  const response = await fetch((path as string) + buildQuery(query as Record<string, unknown> | undefined), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": image.type || "image/jpeg", Accept: "application/json" },
    body: image,
    signal,
  });
  if (response.status === 401) unauthorizedHandler();
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    throw new ApiError(response.status, errorMessage(response.status, body), body);
  }
  return (await response.json()) as ResponseOf<P, "post">;
}

/** URL of a non-JSON resource (MJPEG stream, snapshot, photo): use as an <img> src; the cookie goes with it. */
export function resourceUrl(
  template: string,
  params?: Record<string, unknown>,
  query?: Record<string, unknown>,
): string {
  return buildPath(template, params) + buildQuery(query);
}
