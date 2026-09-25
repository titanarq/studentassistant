import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import { fetchTopics, fetchTopicSummary, topicPath } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

it("encodes the ids in the topic page path", () => {
  expect(topicPath("histo ria", "t/1")).toBe("/subjects/histo%20ria/topics/t%2F1");
});

it("refuses a topic list with fields outside the protocol", async () => {
  stubApi({
    "/api/subjects/historia/topics": jsonResponse({
      subject_id: "historia",
      topics: [{ topic_id: "t", subject_id: "historia", name: "T", colour: "red" }],
    }),
  });

  expect(await fetchTopics("historia")).toEqual({ kind: "error", status: 200 });
});

it("decodes a summary, with a null notes version", async () => {
  const body = {
    subject_id: "historia",
    topic_id: "t",
    sources: { notes: 0, book: 0, pdf: 0, web: 0 },
    sessions: 0,
    session_minutes: 0,
    open_pending: 0,
    notes_version: null,
    generated: [],
  };
  stubApi({ "/api/subjects/historia/topics/t/summary": jsonResponse(body) });

  expect(await fetchTopicSummary("historia", "t")).toEqual({ kind: "ok", value: body });
});

it("maps a 404 without a detail to a generic Spanish one", async () => {
  stubApi({ "/api/subjects/historia/topics/t/summary": new Response("", { status: 404 }) });

  expect(await fetchTopicSummary("historia", "t")).toEqual({ kind: "not-found", detail: "No encontrado." });
});
