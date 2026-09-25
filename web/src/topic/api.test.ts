import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import { fetchBook, saveBook } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

const BOOK = "/api/subjects/historia/topics/revolucion/book";

function book(title: string | null) {
  return jsonResponse({ subject_id: "historia", topic_id: "revolucion", title });
}

it("reads the topic's book title", async () => {
  stubApi({ [BOOK]: book("Historia del mundo contemporáneo") });
  expect(await fetchBook("historia", "revolucion")).toEqual({ kind: "ok", title: "Historia del mundo contemporáneo" });
});

it("reads a topic with no book title", async () => {
  stubApi({ [BOOK]: book(null) });
  expect(await fetchBook("historia", "revolucion")).toEqual({ kind: "ok", title: null });
});

it("saves a title with PUT and answers the stored one", async () => {
  const fetchMock = stubApi({ [`PUT ${BOOK}`]: book("Historia 1º Bachillerato") });
  expect(await saveBook("historia", "revolucion", "Historia  1º Bachillerato")).toEqual({
    kind: "ok",
    title: "Historia 1º Bachillerato",
  });
  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe(BOOK);
  expect(init?.method).toBe("PUT");
  expect(JSON.parse(init?.body as string)).toEqual({ title: "Historia  1º Bachillerato" });
});

it("passes on a 422 with a Spanish detail", async () => {
  stubApi({ [`PUT ${BOOK}`]: jsonResponse({ detail: "El título del libro no puede estar vacío." }, 422) });
  expect(await saveBook("historia", "revolucion", " ")).toEqual({
    kind: "refused",
    status: 422,
    detail: "El título del libro no puede estar vacío.",
  });
});

it("reports a 422 whose detail is not a string as an error", async () => {
  stubApi({ [`PUT ${BOOK}`]: jsonResponse({ detail: [{ msg: "too long" }] }, 422) });
  expect(await saveBook("historia", "revolucion", "x")).toEqual({ kind: "error", status: 422 });
});

it("passes on 404 and 503", async () => {
  stubApi({ [BOOK]: jsonResponse({ detail: "No existe ese tema en la bóveda." }, 404) });
  expect(await fetchBook("historia", "revolucion")).toEqual({
    kind: "refused",
    status: 404,
    detail: "No existe ese tema en la bóveda.",
  });
  stubApi({ [BOOK]: jsonResponse({ detail: "No se puede abrir la bóveda." }, 503) });
  expect(await fetchBook("historia", "revolucion")).toMatchObject({ kind: "refused", status: 503 });
});

it("reports a malformed answer as an error and a network failure as unreachable", async () => {
  stubApi({ [BOOK]: jsonResponse({ subject_id: "historia" }) });
  expect(await fetchBook("historia", "revolucion")).toEqual({ kind: "error", status: 200 });
  stubApi({ [BOOK]: new Error("down") });
  expect(await fetchBook("historia", "revolucion")).toEqual({ kind: "unreachable" });
});
