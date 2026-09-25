import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import { confirmStyleRules, fetchStyleGuide, readStyleGuide, sameRule, saveStyleGuide, styleGuidePagePath } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

const GUIDE = "/api/subjects/historia/style-guide";

function bodyOf(call: unknown[]): unknown {
  return JSON.parse((call[1] as RequestInit).body as string);
}

it("reads the guide leniently", () => {
  expect(readStyleGuide({ subject: "historia", rules: ["Usa tablas.", 3], extra: true })).toEqual({
    subject: "historia",
    rules: ["Usa tablas."],
    added: [],
    commit: null,
  });
  expect(readStyleGuide({ subject: "historia" })).toBeNull();
  expect(readStyleGuide(null)).toBeNull();
});

it("fetches the guide, with the Spanish detail of an unknown subject", async () => {
  stubApi({ [GUIDE]: jsonResponse({ subject: "historia", rules: ["Usa tablas."], added: [], commit: null }) });
  expect(await fetchStyleGuide("historia")).toEqual({
    kind: "ok",
    value: { subject: "historia", rules: ["Usa tablas."], added: [], commit: null },
  });

  stubApi({ [GUIDE]: jsonResponse({ detail: "No existe esa asignatura en la bóveda." }, 404) });
  expect(await fetchStyleGuide("historia")).toEqual({ kind: "not-found", detail: "No existe esa asignatura en la bóveda." });

  stubApi({ [GUIDE]: new TypeError("offline") });
  expect(await fetchStyleGuide("historia")).toEqual({ kind: "unreachable" });
});

it("confirms proposed rules with POST .../rules and saves the list with PUT", async () => {
  const fetchMock = stubApi({
    [`POST ${GUIDE}/rules`]: jsonResponse({ subject: "historia", rules: ["A.", "B."], added: ["B."], commit: "c1" }),
    [`PUT ${GUIDE}`]: jsonResponse({ subject: "historia", rules: [], added: [], commit: "c2" }),
  });

  const confirmed = await confirmStyleRules("historia", ["B."]);
  expect(confirmed).toEqual({ kind: "ok", value: { subject: "historia", rules: ["A.", "B."], added: ["B."], commit: "c1" } });
  expect(fetchMock.mock.calls[0][0]).toBe(`${GUIDE}/rules`);
  expect(bodyOf(fetchMock.mock.calls[0])).toEqual({ rules: ["B."] });

  expect(await saveStyleGuide("historia", [])).toMatchObject({ kind: "ok", value: { rules: [], commit: "c2" } });
  expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("PUT");
  expect(bodyOf(fetchMock.mock.calls[1])).toEqual({ rules: [] });
});

it("reports a refusal's Spanish detail and a validation list as a plain error", async () => {
  stubApi({ [`PUT ${GUIDE}`]: jsonResponse({ detail: "Una regla no puede estar vacía." }, 422) });
  expect(await saveStyleGuide("historia", [" "])).toEqual({
    kind: "refused",
    status: 422,
    detail: "Una regla no puede estar vacía.",
    code: null,
    overCap: false,
  });

  stubApi({ [`PUT ${GUIDE}`]: jsonResponse({ detail: [{ msg: "too long" }] }, 422) });
  expect(await saveStyleGuide("historia", [])).toEqual({ kind: "error", status: 422 });
});

it("compares rules like the backend and builds the page path", () => {
  expect(sameRule("- Usa  tablas.", "usa tablas.")).toBe(true);
  expect(sameRule("Usa tablas.", "Usa esquemas.")).toBe(false);
  expect(styleGuidePagePath("física y química")).toBe("/subjects/f%C3%ADsica%20y%20qu%C3%ADmica/style-guide");
});
