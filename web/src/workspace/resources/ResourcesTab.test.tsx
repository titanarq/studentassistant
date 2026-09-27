import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { parseNotes } from "../../notes/markdown";
import { jsonResponse, stubApi } from "../../test/mockApi";
import ResourcesTab, { RESOURCES_HINT } from "../ResourcesTab";
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

function card(name: RegExp) {
  return screen.getByRole("button", { name });
}

function metaCalls(fetchMock: ReturnType<typeof stubApi>, file: string) {
  return fetchMock.mock.calls.filter(([path]) => path === `${SOURCES}/${file}/meta`).length;
}

it("shows each source's state, the reasons, the counts and the hint", async () => {
  stubApi(ROUTES);
  renderTab();

  expect(await screen.findByText("2 pendientes · 1 incorporada · 4 apartadas")).toBeInTheDocument();
  expect(screen.getByText(RESOURCES_HINT)).toBeInTheDocument();
  const kept = screen.getByRole("list", { name: "Fuentes del tema" });
  // The counts do not wait for page 3's metadata (flagged stays pending): wait for its warning.
  await waitFor(
    () =>
      expect(within(kept).getAllByRole("button").map((b) => b.textContent)).toEqual([
        "Página 1 · apuntesIncorporada",
        "Página 2 · apuntesPendiente",
        "Página 3 · apuntesPendienteAviso: Puede estar cortada",
      ]),
    { timeout: 5000 },
  );

  const toggle = screen.getByRole("button", { name: "Apartadas (4)" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  const setAside = document.getElementById(toggle.getAttribute("aria-controls")!)!;
  expect(setAside).not.toBeVisible();

  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(setAside).toBeVisible();
  expect(within(setAside).getAllByRole("button").map((b) => b.textContent)).toEqual([
    "Página 4 · apuntesApartada En blanco",
    "Página 5 · apuntesApartada Repetida de la página 2",
    "Página 6 · apuntesApartada Mismo contenido que la página 1 (la apartaste tú)",
    "Libro, página 1Apartada Borrosa",
  ]);
  // No incorporate, set-aside or restore buttons: only the sources and the disclosure.
  expect(screen.queryByRole("button", { name: /^(Incorporar|Apartar|Recuperar)/ })).toBeNull();
});

it("opens a source in the viewer, a set-aside one too", async () => {
  stubApi(ROUTES);
  const { onOpen } = renderTab();
  await screen.findByText("2 pendientes · 1 incorporada · 4 apartadas");

  fireEvent.click(card(/Página 2 · apuntes/));
  expect(onOpen).toHaveBeenLastCalledWith({ label: "recurso-notes-2", definition: "[Apuntes, página 2](../sources/notes/page-002.jpg)" });
  fireEvent.click(screen.getByRole("button", { name: "Apartadas (4)" }));
  fireEvent.click(card(/Página 5 · apuntes/));
  expect(onOpen).toHaveBeenLastCalledWith({ label: "recurso-notes-5", definition: "[Apuntes, página 5](../sources/notes/page-005.jpg)" });
});

it("shows page thumbnails, the flattened image first", async () => {
  stubApi(ROUTES);
  renderTab();
  await screen.findByText("2 pendientes · 1 incorporada · 4 apartadas");

  const image = card(/Página 1 · apuntes/).querySelector("img")!;
  expect(image).toHaveAttribute("src", `${SOURCES}/notes/page-001.page.jpg`);
  expect(image).toHaveAttribute("alt", "");
  fireEvent.error(image);
  expect(card(/Página 1 · apuntes/).querySelector("img")).toHaveAttribute("src", `${SOURCES}/notes/page-001.jpg`);
});

it("marks a source incorporada after a notes change, and reads the metadata again", async () => {
  const fetchMock = stubApi(ROUTES);
  const { rerender } = renderTab();
  await screen.findByText("2 pendientes · 1 incorporada · 4 apartadas");
  await waitFor(() => expect(metaCalls(fetchMock, "notes/page-002.jpg")).toBe(1));

  rerender(WITH_P2, 0);

  expect(await screen.findByText("1 pendiente · 2 incorporadas · 4 apartadas")).toBeInTheDocument();
  expect(card(/Página 2 · apuntes/)).toHaveTextContent("Incorporada");
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
  await screen.findByText("2 pendientes · 1 incorporada · 4 apartadas");

  // Same key and notes: nothing is read again.
  rerender(NOTES, 0);
  triaged = true;
  await Promise.resolve();
  expect(metaCalls(fetchMock, "notes/page-002.jpg")).toBe(1);
  expect(screen.getByText("2 pendientes · 1 incorporada · 4 apartadas")).toBeInTheDocument();

  rerender(NOTES, 1);
  expect(await screen.findByText("1 pendiente · 1 incorporada · 5 apartadas")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Apartadas (5)" }));
  expect(card(/Página 2 · apuntes/)).toHaveTextContent("Apartada Borrosa (la apartaste tú)");
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

  expect(await screen.findByText("11 pendientes · 1 incorporada · 0 apartadas")).toBeInTheDocument();
  await waitFor(() => expect(fetchMock.mock.calls.filter(([path]) => String(path).endsWith("/meta"))).toHaveLength(12));
  await waitFor(() => expect(inFlight).toBe(0));
  expect(peak).toBeLessThanOrEqual(META_CONCURRENCY);
  expect(peak).toBeGreaterThan(1);
});

it("keeps legacy sources whose metadata cannot be read, and says when there are none", async () => {
  stubApi({ [SUMMARY]: summary(1) });
  renderTab("# Tema\n\nSin fuentes.\n");
  expect(await screen.findByText("1 pendiente · 0 incorporadas · 0 apartadas")).toBeInTheDocument();
  expect(card(/Página 1 · apuntes/)).toHaveTextContent("Pendiente");
  expect(screen.queryByRole("button", { name: /Apartadas/ })).toBeNull();
});

it("says the topic has no sources yet", async () => {
  stubApi({ [SUMMARY]: summary(0) });
  renderTab("# Tema\n\nNada.\n");
  expect(await screen.findByText("Este tema todavía no tiene fuentes: captura páginas en la pestaña Captura.")).toBeInTheDocument();
  expect(screen.queryByText(RESOURCES_HINT)).toBeNull();
});

it("lists and opens an image pasted into the notes", async () => {
  stubApi({ [SUMMARY]: summary(0) });
  const { onOpen } = renderTab("# Tema\n\nUna figura.[^img001]\n\n[^img001]: [Imagen pegada 1](../sources/images/img-001.png)\n");
  await screen.findByText("0 pendientes · 1 incorporada · 0 apartadas");
  const image = card(/Imagen pegada 1/);
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
