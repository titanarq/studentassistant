/**
 * Client for the web search API (#59, `server/web_search_routes.py`):
 *
 * - `GET  /api/subjects/{s}/topics/{t}/web-searches` lists the topic's searches, newest first;
 * - `POST /api/subjects/{s}/topics/{t}/web-searches` `{query}` queues one (202, it runs in the
 *   background: poll the list until it is no longer `queued`);
 * - `POST .../web-searches/{search_id}/results/{index}/keep` fetches the page and stores it as a
 *   web source of the topic (201).
 *
 * A refusal carries a Spanish `detail` that is shown as it comes.
 */

export interface WebResult {
  url: string;
  title: string;
  summary: string;
  /** Claude judged it worth keeping as a source. */
  relevant: boolean;
  /** Its URL is among the pages the search returned (false: treat it with care). */
  found_in_search: boolean;
}

export interface KeptResult {
  index: number;
  url: string;
  source_id: string;
  kept_by: string;
}

export interface WebSearch {
  search_id: string;
  query: string;
  requested_by: string;
  session_id: string | null;
  queued_at: string;
  status: "queued" | "done" | "failed";
  results: WebResult[];
  reason: string | null;
  message: string | null;
  kept: KeptResult[];
}

export interface KeptSource {
  source_id: string;
  vault_id: string;
  title: string;
  url: string;
}

export type ApiResult<T> =
  | { kind: "ok"; value: T }
  | { kind: "refused"; status: number; detail: string }
  | { kind: "error"; status: number }
  | { kind: "unreachable" };

export function webSearchesPath(subjectId: string, topicId: string): string {
  return `/api/subjects/${encodeURIComponent(subjectId)}/topics/${encodeURIComponent(topicId)}/web-searches`;
}

async function call<T>(url: string, init: RequestInit | undefined, valid: (body: unknown) => body is T): Promise<ApiResult<T>> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    return { kind: "unreachable" };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string" && detail !== "") return { kind: "refused", status: response.status, detail };
    return { kind: "error", status: response.status };
  }
  return valid(body) ? { kind: "ok", value: body } : { kind: "error", status: response.status };
}

function isSearchList(body: unknown): body is { searches: WebSearch[] } {
  const searches = (body as { searches?: unknown } | undefined)?.searches;
  return (
    Array.isArray(searches) &&
    searches.every(
      (s) =>
        typeof s === "object" &&
        s !== null &&
        typeof (s as WebSearch).search_id === "string" &&
        typeof (s as WebSearch).query === "string" &&
        Array.isArray((s as WebSearch).results) &&
        Array.isArray((s as WebSearch).kept),
    )
  );
}

function isQueued(body: unknown): body is { search_id: string } {
  return typeof (body as { search_id?: unknown } | undefined)?.search_id === "string";
}

function isKept(body: unknown): body is KeptSource {
  const b = body as Record<string, unknown> | undefined;
  return typeof b?.source_id === "string" && typeof b?.vault_id === "string" && typeof b?.url === "string";
}

export async function fetchWebSearches(subjectId: string, topicId: string): Promise<ApiResult<WebSearch[]>> {
  const result = await call(webSearchesPath(subjectId, topicId), undefined, isSearchList);
  return result.kind === "ok" ? { kind: "ok", value: result.value.searches } : result;
}

export async function queueWebSearch(subjectId: string, topicId: string, query: string): Promise<ApiResult<string>> {
  const result = await call(
    webSearchesPath(subjectId, topicId),
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query }) },
    isQueued,
  );
  return result.kind === "ok" ? { kind: "ok", value: result.value.search_id } : result;
}

export function keepWebResult(subjectId: string, topicId: string, searchId: string, index: number): Promise<ApiResult<KeptSource>> {
  return call(
    `${webSearchesPath(subjectId, topicId)}/${encodeURIComponent(searchId)}/results/${index}/keep`,
    { method: "POST" },
    isKept,
  );
}

/** A Spanish sentence for a failed call. */
export function describeApiFailure(result: Exclude<ApiResult<unknown>, { kind: "ok" }>): string {
  switch (result.kind) {
    case "refused":
      return result.detail;
    case "error":
      return `el servidor respondió con un error (${result.status}).`;
    case "unreachable":
      return "no se pudo conectar con el servidor.";
  }
}
