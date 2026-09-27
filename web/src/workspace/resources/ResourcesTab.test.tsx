import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parseNotes } from "../../notes/markdown";
import { jsonResponse, stubApi } from "../../test/mockApi";
import { useState } from "react";
import ResourcesTab, { INCORPORATED_LABEL, type OpenResource } from "../ResourcesTab";
import { type SourceSelection, SourceSelectionContext, useSourceSelection } from "./selection";
import { META_CONCURRENCY } from "./useSourceMetas";

const TOPIC = "subjects/historia/topics/revolucion-industrial";
const SOURCES = `/api/sources/${TOPIC}/sources`;
const SUMMARY = "/api/subjects/historia/topics/revolucion-industrial/summary";

const NOTES = `# La Revolución Industrial

## Contexto {#contexto}

Empezó en Gran Bretaña.[^p1]

---

[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)
`;

const WITH_P2 = NOTES.replace("Bretaña.[^p1]", "Bretaña.[^p1][^p2]") + "[^p2]: [Apuntes, página 2](../sources/notes/page-002.jpg)\n";

function summary(notes: number, book = 0) {
  return jsonResponse({
    subject_id: "historia",
    topic_id: "revolucion-industrial",
    sources: { notes, book, pdf: 0, web: 0 },
    sessions: 1,
    session_minutes: 20,
    open_pending: 0,
    notes_version: 1,
    generated: [],
  });
}

function meta(file: string, triage: Record<string, unknown> | null) {
  const vaultId = `${TOPIC}/sources/${file}`;
  return jsonResponse({
    vault_id: vaultId,
    kind: "notes",
    media_type: "image/jpeg",
    size: 10,
    meta: triage === null ? {} : { triage },
    transcription: null,
  });
}

const ROUTES = {
  [SUMMARY]: summary(6, 1),
  [`${SOURCES}/notes/page-001.jpg/meta`]: meta("notes/page-001.jpg", { status: "kept", reasons: [], decided_by: "auto" }),
  [`${SOURCES}/notes/page-002.jpg/meta`]: meta("notes/page-002.jpg", null),
  [`${SOURCES}/notes/page-003.jpg/meta`]: meta("notes/page-003.jpg", { status: "flagged", reasons: ["partial"], decided_by: "auto" }),
  [`${SOURCES}/notes/page-004.jpg/meta`]: meta("notes/page-004.jpg", { status: "set_aside", reasons: ["blank"], decided_by: "auto" }),
  [`${SOURCES}/notes/page-005.jpg/meta`]: meta("notes/page-005.jpg", {
    status: "set_aside",
    reasons: ["duplicate"],
    duplicate_of: "sources/notes/page-002.jpg",
    decided_by: "auto",
  }),
  [`${SOURCES}/notes/page-006.jpg/meta`]: meta("notes/page-006.jpg", {
    status: "set_aside",
    reasons: ["same_content"],
    duplicate_of: "sources/notes/page-001.jpg",
    decided_by: "student",
  }),
  [`${SOURCES}/book/page-001.jpg/meta`]: meta("book/page-001.jpg", { status: "set_aside", reasons: ["blurry"], decided_by: "auto" }),
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const trees = new Map<string, ReturnType<typeof parseNotes>>();
/** The same text gives the same tree, as the workspace's memo does. */
function tree(text: string) {
  if (!trees.has(text)) trees.set(text, parseNotes(text));
  return trees.get(text)!;
}

function renderTab(notes = NOTES, refreshKey = 0, onOpen = vi.fn()) {
  const props = {
    subjectId: "historia",
    topicId: "revolucion-industrial",
    open: null,
    onOpen,
    onClose: () => undefined,
  };
  const view = render(<ResourcesTab {...props} tree={tree(notes)} refreshKey={refreshKey} />);
  return {
    onOpen,
    rerender: (next: string, key: number) => view.rerender(<ResourcesTab {...props} tree={tree(next)} refreshKey={key} />),
  };
}

/** The button that opens a card (not its trash button, #450, whose name also carries the title). */
function card(name: RegExp) {
  const buttons = screen.getAllByRole("button", { name }).filter((b) => b.classList.contains("resource-open"));
  expect(buttons).toHaveLength(1);
  return buttons[0];
}

/** The list of kept sources, once the tab has read it. */
function loaded() {
  return screen.findByRole("list", { name: "Fuentes del tema" });
}

/** The tab once every source of `ROUTES` has its metadata: the four set-aside ones are grouped. */
function loadedAll() {
  return screen.findByRole("button", { name: "Apartadas (4)" }, { timeout: 5000 });
}

/** Whether a card shows the «Incorporada a los apuntes» corner badge (#461). */
function incorporated(button: HTMLElement): boolean {
  return within(button).queryByRole("img", { name: INCORPORATED_LABEL }) !== null;
}

/** The cards' titles in a list, with «+» for the incorporated badge and «!» for a warning. */
function cards(list: HTMLElement): string[] {
  return within(list)
    .getAllByRole("button")
    .filter((b) => b.classList.contains("resource-open"))
    .map((b) => {
      const warn = b.querySelector(".resource-badge-warn")?.getAttribute("aria-label");
      return [b.querySelector(".resource-title")?.textContent, incorporated(b) ? "+" : "", warn ?? "", b.querySelector(".resource-reason")?.textContent ?? ""]
        .filter((part) => part !== "")
        .join(" | ");
    });
}

function metaCalls(fetchMock: ReturnType<typeof stubApi>, file: string) {
  return fetchMock.mock.calls.filter(([path]) => path === `${SOURCES}/${file}/meta`).length;
}

it("shows each source as a thumbnail with a corner badge when incorporated, and no counts or hint (#461)", async () => {
  stubApi(ROUTES);
  renderTab();

  const kept = await loaded();
  // Page 3's warning waits for its metadata.
  await waitFor(
    () =>
      expect(cards(kept)).toEqual([
        "Página 1 · apuntes | +",
        "Página 2 · apuntes",
        "Página 3 · apuntes | Aviso: Puede estar cortada",
      ]),
    { timeout: 5000 },
  );
  // No state chips, no counts line, no hint paragraph (#461).
  expect(screen.queryByText(/Pendiente|pendientes ·/)).toBeNull();
  expect(screen.queryByText(/Selecciona páginas/)).toBeNull();
  expect(within(card(/Página 1 · apuntes/)).getByRole("img", { name: INCORPORATED_LABEL })).toHaveAttribute("title", INCORPORATED_LABEL);

  const toggle = screen.getByRole("button", { name: "Apartadas (4)" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  const setAside = document.getElementById(toggle.getAttribute("aria-controls")!)!;
  expect(setAside).not.toBeVisible();

  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(setAside).toBeVisible();
  expect(cards(setAside)).toEqual([
    "Página 4 · apuntes | En blanco",
    "Página 5 · apuntes | Repetida de la página 2",
    "Página 6 · apuntes | Mismo contenido que la página 1 (la apartaste tú)",
    "Libro, página 1 | Borrosa",
  ]);
  // No incorporate, set-aside or restore buttons: only the sources and the disclosure.
  expect(screen.queryByRole("button", { name: /^(Incorporar|Apartar|Recuperar)/ })).toBeNull();
});

it("opens a source in the viewer, a set-aside one too", async () => {
  stubApi(ROUTES);
  const { onOpen } = renderTab();
  await loadedAll();

  fireEvent.click(card(/Página 2 · apuntes/));
  expect(onOpen).toHaveBeenLastCalledWith({ label: "recurso-notes-2", definition: "[Apuntes, página 2](../sources/notes/page-002.jpg)" });
  fireEvent.click(screen.getByRole("button", { name: "Apartadas (4)" }));
  fireEvent.click(card(/Página 5 · apuntes/));
  expect(onOpen).toHaveBeenLastCalledWith({ label: "recurso-notes-5", definition: "[Apuntes, página 5](../sources/notes/page-005.jpg)" });
});

it("shows page thumbnails, the flattened image first", async () => {
  stubApi(ROUTES);
  renderTab();
  await loaded();

  const image = card(/Página 1 · apuntes/).querySelector("img")!;
  expect(image).toHaveAttribute("src", `${SOURCES}/notes/page-001.page.jpg`);
  expect(image).toHaveAttribute("alt", "");
  fireEvent.error(image);
  expect(card(/Página 1 · apuntes/).querySelector("img")).toHaveAttribute("src", `${SOURCES}/notes/page-001.jpg`);
});

it("marks a source incorporada after a notes change, and reads the metadata again", async () => {
  const fetchMock = stubApi(ROUTES);
  const { rerender } = renderTab();
  await loaded();
  await waitFor(() => expect(metaCalls(fetchMock, "notes/page-002.jpg")).toBe(1));
  expect(incorporated(card(/Página 2 · apuntes/))).toBe(false);

  rerender(WITH_P2, 0);

  await waitFor(() => expect(incorporated(card(/Página 2 · apuntes/))).toBe(true));
  await waitFor(() => expect(metaCalls(fetchMock, "notes/page-002.jpg")).toBe(2));
});

it("picks up a triage change when the tab is shown again, keeping the cache meanwhile", async () => {
  let triaged = false;
  const fetchMock = stubApi({
    ...ROUTES,
    [`${SOURCES}/notes/page-002.jpg/meta`]: () =>
      meta("notes/page-002.jpg", triaged ? { status: "set_aside", reasons: ["blurry"], decided_by: "student" } : null),
  });
  const { rerender } = renderTab();
  await loadedAll();

  // Same key and notes: nothing is read again.
  rerender(NOTES, 0);
  triaged = true;
  await Promise.resolve();
  expect(metaCalls(fetchMock, "notes/page-002.jpg")).toBe(1);
  expect(screen.getByRole("button", { name: "Apartadas (4)" })).toBeInTheDocument();

  rerender(NOTES, 1);
  fireEvent.click(await screen.findByRole("button", { name: "Apartadas (5)" }));
  expect(card(/Página 2 · apuntes/)).toHaveTextContent("Borrosa (la apartaste tú)");
});

it("reads the metadata with bounded concurrency", async () => {
  let inFlight = 0;
  let peak = 0;
  const slow = (file: string) => async () => {
    inFlight++;
    peak = Math.max(peak, inFlight);
    await new Promise((resolve) => setTimeout(resolve, 5));
    inFlight--;
    return meta(file, null);
  };
  const routes: Record<string, Response | (() => Promise<Response>)> = { [SUMMARY]: summary(12) };
  for (let n = 1; n <= 12; n++) {
    const file = `notes/page-${String(n).padStart(3, "0")}.jpg`;
    routes[`${SOURCES}/${file}/meta`] = slow(file);
  }
  const fetchMock = stubApi(routes);
  renderTab();

  expect(cards(await loaded())).toHaveLength(12);
  await waitFor(() => expect(fetchMock.mock.calls.filter(([path]) => String(path).endsWith("/meta"))).toHaveLength(12));
  await waitFor(() => expect(inFlight).toBe(0));
  expect(peak).toBeLessThanOrEqual(META_CONCURRENCY);
  expect(peak).toBeGreaterThan(1);
});

it("keeps legacy sources whose metadata cannot be read, and says when there are none", async () => {
  stubApi({ [SUMMARY]: summary(1) });
  renderTab("# Tema\n\nSin fuentes.\n");
  await loaded();
  expect(incorporated(card(/Página 1 · apuntes/))).toBe(false);
  expect(screen.queryByRole("button", { name: /Apartadas/ })).toBeNull();
});

it("says the topic has no sources yet", async () => {
  stubApi({ [SUMMARY]: summary(0) });
  renderTab("# Tema\n\nNada.\n");
  expect(
    await screen.findByText("Este tema todavía no tiene fuentes: captura páginas en la pestaña Captura o añádelas abajo."),
  ).toBeInTheDocument();
});

it("lists and opens an image pasted into the notes", async () => {
  stubApi({ [SUMMARY]: summary(0) });
  const { onOpen } = renderTab("# Tema\n\nUna figura.[^img001]\n\n[^img001]: [Imagen pegada 1](../sources/images/img-001.png)\n");
  await loaded();
  const image = card(/Imagen pegada 1/);
  expect(incorporated(image)).toBe(true);
  expect(image.querySelector("img")).toHaveAttribute("src", `/api/sources/${TOPIC}/sources/images/img-001.png`);
  fireEvent.click(image);
  expect(onOpen).toHaveBeenCalledWith({ label: "img001", definition: "[Imagen pegada 1](../sources/images/img-001.png)" });
});

it("shows a pasted image in the viewer", () => {
  stubApi({ [SUMMARY]: summary(0) });
  render(
    <ResourcesTab
      subjectId="historia"
      topicId="revolucion-industrial"
      tree={null}
      refreshKey={0}
      open={{ label: "img001", definition: "[Imagen pegada 1](../sources/images/img-001.png)" }}
      onOpen={() => undefined}
      onClose={() => undefined}
    />,
  );
  const dialog = screen.getByRole("dialog", { name: "Imagen pegada 1" });
  expect(within(dialog).getByRole("img", { name: "Imagen pegada 1" })).toHaveAttribute(
    "src",
    `/api/sources/${TOPIC}/sources/images/img-001.png`,
  );
});

describe("the selection (#432)", () => {
  const held: { current: SourceSelection | null } = { current: null };

  /** The tab inside a workspace-like selection, opening and closing the viewer itself. */
  function Selecting({ refreshKey }: { refreshKey: number }) {
    const selection = useSourceSelection();
    held.current = selection;
    const [open, setOpen] = useState<OpenResource | null>(null);
    return (
      <SourceSelectionContext.Provider value={selection}>
        <ResourcesTab
          subjectId="historia"
          topicId="revolucion-industrial"
          tree={tree(NOTES)}
          refreshKey={refreshKey}
          open={open}
          onOpen={setOpen}
          onClose={() => setOpen(null)}
        />
      </SourceSelectionContext.Provider>
    );
  }

  async function renderSelecting() {
    const view = render(<Selecting refreshKey={0} />);
    await loadedAll();
    return { rerender: (key: number) => view.rerender(<Selecting refreshKey={key} />) };
  }

  const box = (name: string) => screen.getByRole("checkbox", { name: `Seleccionar ${name}` });
  const selectedIds = () => held.current!.selected.map((s) => s.id);

  it("gives each stored source a checkbox apart from the button that opens it", async () => {
    stubApi(ROUTES);
    await renderSelecting();
    const kept = screen.getByRole("list", { name: "Fuentes del tema" });
    expect(within(kept).getAllByRole("checkbox")).toHaveLength(3);
    const checkbox = box("Página 2 · apuntes");
    expect(card(/Página 2 · apuntes/)).not.toContainElement(checkbox);
    // The image still opens the source, without selecting it.
    fireEvent.click(card(/Página 2 · apuntes/).querySelector("img")!);
    expect(await screen.findByRole("button", { name: "Cerrar" })).toBeInTheDocument();
    expect(selectedIds()).toEqual([]);
  });

  it("selects one and several sources, with a count, a selected style and «Quitar selección»", async () => {
    stubApi(ROUTES);
    await renderSelecting();
    expect(screen.queryByRole("button", { name: "Quitar selección" })).toBeNull();

    fireEvent.click(box("Página 2 · apuntes"));
    expect(box("Página 2 · apuntes")).toBeChecked();
    expect(screen.getByText("1 seleccionada")).toBeInTheDocument();
    expect(box("Página 2 · apuntes").closest("li")).toHaveClass("resource-selected");
    expect(selectedIds()).toEqual(["sources/notes/page-002.jpg"]);

    fireEvent.click(screen.getByRole("button", { name: "Apartadas (4)" }));
    fireEvent.click(box("Libro, página 1"));
    expect(screen.getByText("2 seleccionadas")).toBeInTheDocument();
    expect(held.current!.selected).toEqual([
      { id: "sources/notes/page-002.jpg", title: "Pág. 2" },
      { id: "sources/book/page-001.jpg", title: "Libro p. 1" },
    ]);

    fireEvent.click(box("Página 2 · apuntes"));
    expect(box("Página 2 · apuntes")).not.toBeChecked();
    expect(box("Página 2 · apuntes").closest("li")).not.toHaveClass("resource-selected");
    expect(screen.getByText("1 seleccionada")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Quitar selección" }));
    expect(selectedIds()).toEqual([]);
    expect(box("Libro, página 1")).not.toBeChecked();
    expect(screen.queryByRole("button", { name: "Quitar selección" })).toBeNull();
  });

  it("selects the range from the last toggled card with Shift", async () => {
    stubApi(ROUTES);
    await renderSelecting();
    fireEvent.click(screen.getByRole("button", { name: "Apartadas (4)" }));
    fireEvent.click(box("Página 2 · apuntes"));
    fireEvent.click(box("Página 5 · apuntes"), { shiftKey: true });
    expect(selectedIds()).toEqual([
      "sources/notes/page-002.jpg",
      "sources/notes/page-003.jpg",
      "sources/notes/page-004.jpg",
      "sources/notes/page-005.jpg",
    ]);
    expect(screen.getByText("4 seleccionadas")).toBeInTheDocument();
  });

  it("keeps the selection across the viewer, and drops sources no longer listed after a reload", async () => {
    stubApi(ROUTES);
    const { rerender } = await renderSelecting();
    fireEvent.click(box("Página 1 · apuntes"));
    fireEvent.click(box("Página 3 · apuntes"));

    fireEvent.click(card(/Página 1 · apuntes/));
    fireEvent.click(await screen.findByRole("button", { name: "Cerrar" }));
    expect(box("Página 1 · apuntes")).toBeChecked();
    expect(box("Página 3 · apuntes")).toBeChecked();

    // Page 3 is gone from the topic: the next read of the list drops it from the selection.
    stubApi({ ...ROUTES, [SUMMARY]: summary(2) });
    rerender(1);
    await waitFor(() => expect(selectedIds()).toEqual(["sources/notes/page-001.jpg"]));
    expect(screen.getByText("1 seleccionada")).toBeInTheDocument();
  });

  it("offers no checkbox outside a workspace page", async () => {
    stubApi(ROUTES);
    renderTab();
    await loaded();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });
});

describe("deleting a source (#450)", () => {
  const PAGE_1 = `DELETE ${SOURCES}/notes/page-001.jpg`;

  it("asks inline, deletes on «Borrar» and reads the list again", async () => {
    let summaries = 0;
    const fetchMock = stubApi({
      ...ROUTES,
      [SUMMARY]: () => (summaries++ === 0 ? summary(2) : summary(1)),
      [PAGE_1]: () => new Response(null, { status: 204 }),
    });
    renderTab();
    await screen.findByRole("button", { name: /^Página 2/ });
    const confirmSpy = vi.spyOn(window, "confirm");

    fireEvent.click(screen.getByRole("button", { name: "Borrar Página 1 · apuntes" }));

    const ask = screen.getByRole("group", { name: "Borrar Página 1 · apuntes" });
    expect(ask).toHaveTextContent("¿Borrar esta fuente?");
    expect(within(ask).getByRole("button", { name: "Cancelar" })).toHaveFocus();
    expect(confirmSpy).not.toHaveBeenCalled();
    fireEvent.click(within(ask).getByRole("button", { name: "Borrar" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => `${init?.method} ${path}` === PAGE_1)).toBe(true));
    await waitFor(() => expect(summaries).toBe(2));
    await waitFor(() => expect(screen.queryByRole("group", { name: /^Borrar/ })).toBeNull());
  });

  it("cancels with «Cancelar» or Escape, giving the focus back to the trash button", async () => {
    const fetchMock = stubApi({ ...ROUTES, [SUMMARY]: summary(2) });
    renderTab();
    const trash = await screen.findByRole("button", { name: "Borrar Página 1 · apuntes" });

    fireEvent.click(trash);
    fireEvent.click(within(screen.getByRole("group", { name: "Borrar Página 1 · apuntes" })).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("group", { name: "Borrar Página 1 · apuntes" })).toBeNull();
    expect(screen.getByRole("button", { name: "Borrar Página 1 · apuntes" })).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: "Borrar Página 1 · apuntes" }));
    fireEvent.keyDown(screen.getByRole("button", { name: "Cancelar" }), { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Borrar Página 1 · apuntes" })).toBeNull();
    expect(screen.getByRole("button", { name: "Borrar Página 1 · apuntes" })).toHaveFocus();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  });

  it("says when the server cannot delete sources yet, and the backend's detail of a refusal", async () => {
    stubApi({
      ...ROUTES,
      [SUMMARY]: summary(2),
      [PAGE_1]: () => jsonResponse({ detail: "Method Not Allowed" }, 405),
      [`DELETE ${SOURCES}/notes/page-002.jpg`]: () => jsonResponse({ detail: "Los apuntes citan esta página." }, 409),
    });
    renderTab();

    fireEvent.click(await screen.findByRole("button", { name: "Borrar Página 1 · apuntes" }));
    fireEvent.click(screen.getByRole("button", { name: "Borrar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo borrar: Este servidor todavía no permite borrar fuentes.");
    fireEvent.click(screen.getByRole("button", { name: "Cerrar" }));
    expect(screen.queryByRole("alert")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Borrar Página 2 · apuntes" }));
    fireEvent.click(screen.getByRole("button", { name: "Borrar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo borrar: Los apuntes citan esta página.");
    // The source stays listed.
    expect(card(/^Página 2/)).toBeInTheDocument();
  });
});

describe("a retired source (#451, #456)", () => {
  const LIST = `/api/subjects/historia/topics/revolucion-industrial/sources`;
  const listed = (...files: string[]) =>
    jsonResponse({
      subject_id: "historia",
      topic_id: "revolucion-industrial",
      sources: files.map((file) => ({ vault_id: `${TOPIC}/sources/${file}`, kind: file.split("/")[0], title: null })),
    });

  it("leaves the tab after a 204, even when the notes still cite it", async () => {
    let deleted = false;
    const fetchMock = stubApi({
      ...ROUTES,
      [LIST]: () => (deleted ? listed("notes/page-002.jpg") : listed("notes/page-001.jpg", "notes/page-002.jpg")),
      [`${SOURCES}/notes/page-001.jpg/meta`]: () =>
        deleted
          ? jsonResponse({
              vault_id: `${TOPIC}/sources/notes/page-001.jpg`,
              kind: "notes",
              media_type: "image/jpeg",
              size: 10,
              meta: { removed: { at: "2026-09-27T20:00:00Z", by: "student" } },
              transcription: null,
              removed: true,
            })
          : meta("notes/page-001.jpg", null),
      [`DELETE ${SOURCES}/notes/page-001.jpg`]: () => {
        deleted = true;
        return new Response(null, { status: 204 });
      },
    });
    renderTab();
    await loaded();
    expect(incorporated(card(/Página 1 · apuntes/))).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Borrar Página 1 · apuntes" }));
    fireEvent.click(screen.getByRole("button", { name: "Borrar" }));

    // Page 1 is cited by the notes' [^p1], but the list no longer has it: it is not listed again.
    await waitFor(() => expect(cards(screen.getByRole("list", { name: "Fuentes del tema" }))).toEqual(["Página 2 · apuntes"]));
    // Still out once the list and the metadata were read again (the notes cite it, its meta says removed).
    await waitFor(() => expect(metaCalls(fetchMock, "notes/page-001.jpg")).toBeGreaterThan(1));
    expect(cards(screen.getByRole("list", { name: "Fuentes del tema" }))).toEqual(["Página 2 · apuntes"]);
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === "DELETE");
    expect(call?.[0]).toBe(`${SOURCES}/notes/page-001.jpg`);
  });

  it("is not listed when its metadata says removed", async () => {
    stubApi({
      [SUMMARY]: summary(2),
      [`${SOURCES}/notes/page-001.jpg/meta`]: jsonResponse({
        vault_id: `${TOPIC}/sources/notes/page-001.jpg`,
        kind: "notes",
        media_type: "image/jpeg",
        size: 10,
        meta: { removed: { at: "2026-09-27T20:00:00Z", by: "student" } },
        transcription: null,
        removed: true,
      }),
      [`${SOURCES}/notes/page-002.jpg/meta`]: meta("notes/page-002.jpg", null),
    });
    renderTab();
    await loaded();
    await waitFor(() => expect(cards(screen.getByRole("list", { name: "Fuentes del tema" }))).toEqual(["Página 2 · apuntes"]));
  });
});
