import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import {
  answerDoubt,
  decodeDoubtsQueue,
  describeActionFailure,
  describeReview,
  dismissDoubt,
  fetchDoubts,
  reviewDoubts,
} from "./doubts";
import { doubt, doubts, question, resolution } from "./testPending";

afterEach(() => {
  vi.unstubAllGlobals();
});

const BASE = "/api/subjects/historia/topics/revolucion-francesa/doubts";

const OUTCOME = {
  pending_id: "p2",
  status: "resolved",
  resolution: "Vale 1789.",
  evidence: [],
  answer: null,
  suggestion: null,
  chosen_source: { source_id: "sources/notes/page-001.jpg", says: "1789" },
  discarded: [{ source_id: "sources/book/page-004.jpg", says: "1791" }],
  keep_discarded: true,
  notes_changed: true,
  warning: null,
  resolved_at: { session_id: "2026-09-25-0900", seq: 3 },
};

it("decodes the queue with questions and outcomes", async () => {
  const body = doubts([
    doubt({}, question()),
    { ...doubt({ id: "p2", kind: "contradiction", status: "resolved" }), outcome: OUTCOME },
  ]);
  stubApi({ [BASE]: jsonResponse(body) });

  const result = await fetchDoubts("historia", "revolucion-francesa");

  expect(result).toEqual({ kind: "ok", value: body });
});

it("rejects a queue with an unknown field and reports 404 with its detail", async () => {
  expect(() => decodeDoubtsQueue({ ...doubts([]), extra: 1 }, "")).toThrow();
  stubApi({ [BASE]: jsonResponse({ detail: "No existe ese tema en la bóveda." }, 404) });
  expect(await fetchDoubts("historia", "revolucion-francesa")).toEqual({
    kind: "not-found",
    detail: "No existe ese tema en la bóveda.",
  });
});

it("posts an answer with confirm_over_cap and reads the resolution", async () => {
  const fetchMock = stubApi({ [`POST ${BASE}/p1/answer`]: jsonResponse(resolution()) });

  const result = await answerDoubt("historia", "revolucion-francesa", "p1", { suggestion: 2 });

  expect(result).toEqual({
    kind: "ok",
    value: {
      pending_id: "p1",
      status: "resolved",
      resolution: "La fecha es el 14 de julio de 1789.",
      notes_changed: true,
      warning: null,
    },
  });
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  expect(JSON.parse(init.body as string)).toEqual({ suggestion: 2, confirm_over_cap: false });
});

it("dismisses without a body", async () => {
  const fetchMock = stubApi({
    [`POST ${BASE}/p%2F1/dismiss`]: jsonResponse(resolution({ pending_id: "p/1", status: "dismissed" })),
  });

  const result = await dismissDoubt("historia", "revolucion-francesa", "p/1");

  expect(result.kind).toBe("ok");
  expect((fetchMock.mock.calls[0][1] as RequestInit).body).toBeUndefined();
});

it("keeps a refusal's Spanish detail and code, and marks a reached cost cap by its code", async () => {
  const cap = "Se ha alcanzado el límite de gasto del día (5.10 de 5.00 USD). Confirma para continuar igualmente.";
  stubApi({
    [`POST ${BASE}/review`]: jsonResponse({ detail: cap, code: "cost_cap_reached" }, 409),
    [`POST ${BASE}/p1/answer`]: jsonResponse({ detail: "Esa duda ya está cerrada.", code: "doubt_closed" }, 409),
    [`POST ${BASE}/p1/dismiss`]: new Error("offline"),
  });

  const refused = await reviewDoubts("historia", "revolucion-francesa");
  expect(refused).toEqual({
    kind: "refused",
    status: 409,
    detail: cap,
    code: "cost_cap_reached",
    overCap: true,
  });
  const closed = await answerDoubt("historia", "revolucion-francesa", "p1", { answer: "1789" });
  expect(closed).toEqual({
    kind: "refused",
    status: 409,
    detail: "Esa duda ya está cerrada.",
    code: "doubt_closed",
    overCap: false,
  });
  const offline = await dismissDoubt("historia", "revolucion-francesa", "p1");
  expect(offline.kind === "ok" ? "" : describeActionFailure(offline)).toBe("No se pudo conectar con el servidor.");
});

it("goes by the code, never by the wording of the detail", async () => {
  const reworded = "Has llegado al tope de gasto de hoy.";
  const lookalike = "Se ha alcanzado el límite de gasto del día (5.10 de 5.00 USD).";
  let calls = 0;
  stubApi({
    [`POST ${BASE}/review`]: () =>
      ++calls === 1
        ? jsonResponse({ detail: reworded, code: "cost_cap_reached" }, 409)
        : jsonResponse({ detail: lookalike, code: "added_later" }, 409),
  });

  expect(await reviewDoubts("historia", "revolucion-francesa")).toMatchObject({ code: "cost_cap_reached", overCap: true });
  expect(await reviewDoubts("historia", "revolucion-francesa")).toMatchObject({ code: null, overCap: false });
});

it("says what a review did", () => {
  const review = { auto_resolved: ["a"], asked: ["b", "c"], notes_changed: true, warning: null };
  expect(describeReview(review)).toBe("El editor ha resuelto 1 duda con tus fuentes y tiene 2 preguntas para ti.");
  expect(describeReview({ ...review, auto_resolved: [], asked: [] })).toBe("No había dudas abiertas que revisar.");
});
