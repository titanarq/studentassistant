import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import { decodeTopicPending, fetchPending, kindLabel, pendingPath, statusLabel } from "./api";
import { item, queue } from "./testPending";

afterEach(() => {
  vi.unstubAllGlobals();
});

const PATH = "/api/subjects/historia/topics/revolucion-francesa/pending?status=open";

it("builds the pending path with the filter", () => {
  expect(pendingPath("historia", "revolución", "all")).toBe(
    "/api/subjects/historia/topics/revoluci%C3%B3n/pending?status=all",
  );
});

it("decodes the server's TopicPending", async () => {
  const body = queue([
    item({ refs: { pages: ["c1"], segments: ["s1", "s2"], sources: [] }, merged_ids: ["p9"] }),
    item({ id: "p2", status: "resolved", resolution: "1789", resolved_at: { session_id: "x", seq: 9 } }),
  ]);
  stubApi({ [PATH]: jsonResponse(body) });

  expect(await fetchPending("historia", "revolucion-francesa", "open")).toEqual({ kind: "ok", value: body });
});

it("fills refs lists the server left out", () => {
  const body = { ...queue([]), items: [{ ...item(), refs: { pages: ["c1"] } }] };
  expect(decodeTopicPending(body, "").items[0].refs).toEqual({ pages: ["c1"], segments: [], sources: [] });
});

it("keeps an unknown kind or status and labels it generically", () => {
  const body = queue([item({ kind: "new_kind", status: "later" })]);
  const decoded = decodeTopicPending(body, "");
  expect(decoded.items[0].kind).toBe("new_kind");
  expect(kindLabel("new_kind")).toBe("Otra duda");
  expect(statusLabel("dismissed")).toBe("Descartada");
});

it("rejects a body with unknown fields as an error", async () => {
  stubApi({ [PATH]: jsonResponse({ ...queue([]), extra: 1 }) });
  expect(await fetchPending("historia", "revolucion-francesa", "open")).toEqual({ kind: "error", status: 200 });
});

it("reports a 404 with its detail, a 500 and a network failure", async () => {
  stubApi({ [PATH]: jsonResponse({ detail: "Tema desconocido." }, 404) });
  expect(await fetchPending("historia", "revolucion-francesa", "open")).toEqual({
    kind: "not-found",
    detail: "Tema desconocido.",
  });

  stubApi({ [PATH]: jsonResponse({}, 500) });
  expect(await fetchPending("historia", "revolucion-francesa", "open")).toEqual({ kind: "error", status: 500 });

  stubApi({ [PATH]: new Error("down") });
  expect(await fetchPending("historia", "revolucion-francesa", "open")).toEqual({ kind: "unreachable" });
});
