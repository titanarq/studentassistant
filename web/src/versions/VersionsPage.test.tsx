import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import VersionsPage, { defaultComparison } from "./VersionsPage";
import { readVersions } from "./api";
import { section, version, versionDiff, versions } from "./testVersions";

afterEach(() => {
  vi.unstubAllGlobals();
});

const BASE = "/api/subjects/historia/topics/revolucion-francesa/notes/versions";
const TOPICS = {
  "/api/subjects/historia/topics": jsonResponse({
    subject_id: "historia",
    topics: [{ topic_id: "revolucion-francesa", subject_id: "historia", name: "La Revolución francesa" }],
  }),
};

const CAUSAS_DIFF =
  "--- apuntes v2 #causas\n+++ apuntes v3 #causas\n@@ -1,3 +1,3 @@\n ## Causas {#causas}\n \n-La crisis de 1788.\n+La crisis fiscal de 1788.\n";

function renderPage() {
  render(<VersionsPage subjectId="historia" topicId="revolucion-francesa" />);
}

it("lists the versions newest first and compares the latest two by section", async () => {
  stubApi({
    ...TOPICS,
    [BASE]: jsonResponse(versions([version(1), version(2), version(3, { current: true })])),
    [`${BASE}/diff?from=2&to=3`]: jsonResponse(
      versionDiff(2, 3, [
        section(""),
        section("causas", { status: "changed", diff: CAUSAS_DIFF }),
        section("fases", { status: "added", diff: "--- a\n+++ b\n@@ -0,0 +1 @@\n+## Fases {#fases}\n" }),
        section("contexto"),
      ], { footnotes: { added: ["p4"], removed: [], changed: ["p1"] } }),
    ),
  });
  renderPage();

  expect(await screen.findByRole("heading", { name: "Versiones de los apuntes de La Revolución francesa" })).toBeInTheDocument();
  const list = screen.getAllByRole("listitem").filter((li) => li.getAttribute("aria-label")?.startsWith("Versión"));
  expect(list.map((li) => li.getAttribute("aria-label"))).toEqual(["Versión 3", "Versión 2", "Versión 1"]);
  expect(list[0]).toHaveTextContent("la de los apuntes actuales");
  expect(within(list[0]).queryByRole("button")).not.toBeInTheDocument();
  expect(within(list[1]).getByRole("button", { name: "Restaurar la versión 2" })).toBeInTheDocument();
  expect(list[1]).toHaveTextContent("Apuntes v2 de historia/revolucion-francesa");

  const causas = await screen.findByRole("article", { name: "causas: Modificada" });
  expect(within(causas).getByText("La crisis de 1788.").closest("del")).not.toBeNull();
  expect(within(causas).getByText("La crisis fiscal de 1788.").closest("ins")).not.toBeNull();
  expect(causas).toHaveTextContent("1 línea añadida, 1 quitada");
  expect(screen.getByRole("article", { name: "fases: Nueva" })).toBeInTheDocument();
  expect(screen.queryByRole("article", { name: /contexto/ })).not.toBeInTheDocument();
  expect(screen.getByText("2 secciones sin cambios")).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Fuentes citadas" })).toHaveTextContent("Nuevas: [^p4]Modificadas: [^p1]");
  expect(screen.getByRole("link", { name: "← Tema La Revolución francesa" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa",
  );
});

it("shows a section side by side, the older text on the left", async () => {
  stubApi({
    ...TOPICS,
    [BASE]: jsonResponse(versions([version(2), version(3, { current: true })])),
    [`${BASE}/diff?from=2&to=3`]: jsonResponse(versionDiff(2, 3, [section("causas", { status: "changed", diff: CAUSAS_DIFF })])),
  });
  renderPage();
  await screen.findByRole("article", { name: "causas: Modificada" });

  fireEvent.click(screen.getByRole("radio", { name: "Lado a lado" }));

  const table = within(screen.getByRole("article", { name: "causas: Modificada" })).getByRole("table");
  expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Versión 2", "Versión 3"]);
  const changed = within(table).getByText("La crisis de 1788.").closest("tr")!;
  expect(within(changed).getAllByRole("cell").map((c) => c.textContent)).toEqual([
    "La crisis de 1788.",
    "La crisis fiscal de 1788.",
  ]);
});

it("compares the current notes with the latest version when they changed after it", async () => {
  const fetchMock = stubApi({
    ...TOPICS,
    [BASE]: jsonResponse(versions([version(1), version(2)], { changed_since_latest: true })),
    [`${BASE}/diff?from=2`]: jsonResponse(
      versionDiff(2, null, [section("causas", { status: "changed", renamed: true, moved: true, title_before: "Orígenes", title_after: "Causas", diff: CAUSAS_DIFF })]),
    ),
    [`${BASE}/diff?from=1&to=2`]: jsonResponse(versionDiff(1, 2, [], { identical: true })),
  });
  renderPage();

  const article = await screen.findByRole("article", { name: "Causas: Modificada" });
  expect(article).toHaveTextContent("Sección renombrada, antes «Orígenes», cambiada de sitio.");
  expect(screen.getByText("Los apuntes actuales tienen cambios posteriores a la versión 2.")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Hasta" })).toHaveValue("current");

  fireEvent.change(screen.getByRole("combobox", { name: "Desde" }), { target: { value: "1" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Hasta" }), { target: { value: "2" } });

  expect(await screen.findByText("Versión 1 y versión 2 son iguales.")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(`${BASE}/diff?from=1&to=2`);
});

it("restores a version after a confirmation and reads the history again", async () => {
  let restored = false;
  const fetchMock = stubApi({
    ...TOPICS,
    [BASE]: () =>
      jsonResponse(
        restored
          ? versions([version(1), version(2), version(3, { current: true })])
          : versions([version(1), version(2, { current: true })]),
      ),
    [`${BASE}/diff?from=1&to=2`]: jsonResponse(versionDiff(1, 2, [])),
    [`${BASE}/diff?from=2&to=3`]: jsonResponse(versionDiff(2, 3, [])),
    [`POST ${BASE}/1/restore`]: () => {
      restored = true;
      return jsonResponse({
        subject: "historia",
        topic: "revolucion-francesa",
        restored_version: 1,
        version: 3,
        tag: "historia/revolucion-francesa/apuntes-v3",
        commit: "abc",
        path: "subjects/historia/topics/revolucion-francesa/notes/apuntes.md",
        diff: "",
        notes: "# La Revolución francesa\n",
        errors: ["[^p9]: la fuente ya no existe"],
        warning: "Algunas fuentes citadas ya no están en el tema.",
      });
    },
  });
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "Restaurar la versión 1" }));
  const confirm = screen.getByRole("group", { name: "Confirmar la restauración de la versión 1" });
  expect(confirm).toHaveTextContent("No se pierde ninguna versión.");
  expect(fetchMock).not.toHaveBeenCalledWith(`${BASE}/1/restore`, expect.anything());
  fireEvent.click(within(confirm).getByRole("button", { name: "Sí, restaurar" }));

  expect(await screen.findByText("Se ha restaurado la versión 1 como versión 3.")).toBeInTheDocument();
  expect(screen.getByText("Algunas fuentes citadas ya no están en el tema.")).toBeInTheDocument();
  expect(await screen.findByRole("listitem", { name: "Versión 3" })).toHaveTextContent("la de los apuntes actuales");
  expect(fetchMock).toHaveBeenCalledWith(`${BASE}/1/restore`, { method: "POST" });
  expect(fetchMock).toHaveBeenCalledWith(`${BASE}/diff?from=2&to=3`);
});

it("cancels a restore without calling the backend", async () => {
  const fetchMock = stubApi({
    ...TOPICS,
    [BASE]: jsonResponse(versions([version(1), version(2, { current: true })])),
    [`${BASE}/diff?from=1&to=2`]: jsonResponse(versionDiff(1, 2, [])),
  });
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "Restaurar la versión 1" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));

  expect(screen.getByRole("button", { name: "Restaurar la versión 1" })).toBeInTheDocument();
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

it("shows the backend's reason when a restore is refused", async () => {
  stubApi({
    ...TOPICS,
    [BASE]: jsonResponse(versions([version(1), version(2, { current: true })])),
    [`${BASE}/diff?from=1&to=2`]: jsonResponse(versionDiff(1, 2, [])),
    [`POST ${BASE}/1/restore`]: jsonResponse(
      { detail: "El editor ya está trabajando en los apuntes o las dudas de este tema." },
      409,
    ),
  });
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "Restaurar la versión 1" }));
  fireEvent.click(screen.getByRole("button", { name: "Sí, restaurar" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "No se pudo restaurar la versión: El editor ya está trabajando en los apuntes o las dudas de este tema.",
  );
});

it("explains a topic without versions and one with nothing to compare yet", async () => {
  stubApi({ ...TOPICS, [BASE]: jsonResponse(versions([], { has_notes: false })) });
  const { unmount } = render(<VersionsPage subjectId="historia" topicId="revolucion-francesa" />);
  expect(await screen.findByText(/Todavía no hay versiones de los apuntes de este tema/)).toBeInTheDocument();
  unmount();

  stubApi({ ...TOPICS, [BASE]: jsonResponse(versions([version(1, { current: true })])) });
  renderPage();
  expect(await screen.findByText(/todavía no hay nada que comparar/)).toBeInTheDocument();
});

it("shows the backend's detail for an unknown topic", async () => {
  stubApi({ ...TOPICS, [BASE]: jsonResponse({ detail: "No existe ese tema en la bóveda." }, 404) });
  renderPage();

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudieron cargar las versiones: No existe ese tema en la bóveda.");
});

it("picks the default comparison", () => {
  const list = (items: unknown[], extra = {}) => readVersions(versions(items, extra))!;
  expect(defaultComparison(list([]))).toBeNull();
  expect(defaultComparison(list([version(1)]))).toBeNull();
  expect(defaultComparison(list([version(1)], { changed_since_latest: true }))).toEqual({ from: 1, to: null });
  expect(defaultComparison(list([version(1), version(2)]))).toEqual({ from: 1, to: 2 });
});
