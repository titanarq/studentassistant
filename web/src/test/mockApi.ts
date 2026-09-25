import { vi } from "vitest";

/** A JSON `Response`, as the backend answers. */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

type Route = Response | (() => Response | Promise<Response>) | Error;

/**
 * Stubs `fetch` with a table of `"METHOD path"` (or just `path` for GET) -> answer; an `Error`
 * answer rejects like a network failure, and any path not in the table answers 500.
 */
export function stubApi(routes: Record<string, Route>) {
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const route = routes[`${method} ${input}`] ?? (method === "GET" ? routes[input] : undefined);
    if (route === undefined) return jsonResponse({ detail: `unexpected ${method} ${input}` }, 500);
    if (route instanceof Error) throw route;
    return typeof route === "function" ? route() : route.clone();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
