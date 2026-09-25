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

/** One Server-Sent Event as the backend writes it. */
export function sseEvent(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * A `text/event-stream` response fed by the test: `push` enqueues raw text (an event, or part of
 * one), `close` ends the stream and `fail` breaks it like a dropped connection.
 */
export function streamResponse() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    push: (text: string) => controller.enqueue(encoder.encode(text)),
    close: () => controller.close(),
    fail: () => controller.error(new TypeError("network error")),
  };
}

/** A complete `text/event-stream` response made of `events`. */
export function sseResponse(events: [string, unknown][]): Response {
  return new Response(events.map(([event, data]) => sseEvent(event, data)).join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}
