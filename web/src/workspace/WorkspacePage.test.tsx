import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installCaptureFakes, swapProperty, type CaptureFakes } from "../capture/testing";
import { revision } from "../chat/testChat";
import { NOTES } from "../notes/testNotes";
import { PROTOCOL_VERSION } from "../protocol";
import { jsonResponse, sseEvent, streamResponse, stubApi } from "../test/mockApi";
import { resourceList } from "./resources";
import WorkspacePage, { EMPTY_NOTES } from "./WorkspacePage";
import { ConfirmProvider } from "../ui/ConfirmDialog";
import { parseNotes } from "../notes/markdown";
import { PAGE_TEST_TIMEOUT } from "../test/timeouts";

const BASE = "/api/subjects/historia/topics/revolucion-industrial";
const TOPIC = "subjects/historia/topics/revolucion-industrial";
const SOURCES = `/api/sources/${TOPIC}/sources`;
const NOW = 1790251200000;

const SESSION = {
  session_id: "s-20260926-1000",
  subject_id: "historia",
  topic_id: "revolucion-industrial",
  status: "active",
  started_at_ms: NOW,
  ws_path: "/ws/sessions/s-20260926-1000",
  protocol_version: PROTOCOL_VERSION,
  received_capture_ids: [],
};

function notes(text = NOTES, extra: Record<string, unknown> = {}) {
  return jsonResponse({ subject_id: "historia", topic_id: "revolucion-industrial", text, version: 2, ...extra });
}

function meta(vaultId: string, kind: string, sidecar: Record<string, unknown> | null, transcription: string | null = null) {
  return jsonResponse({ vault_id: vaultId, kind, media_type: "image/jpeg", size: 10, meta: sidecar, transcription });
}

const ROUTES = {
  "/api/subjects": jsonResponse({ subjects: [{ subject_id: "historia", name: "Historia" }] }),
  "/api/subjects/historia/topics": jsonResponse({
    subject_id: "historia",
    topics: [{ topic_id: "revolucion-industrial", subject_id: "historia", name: "La Revolución Industrial" }],
  }),
  [`${BASE}/notes`]: notes(),
  [`${BASE}/notes/chat`]: jsonResponse({ subject: "historia", topic: "revolucion-industrial", turns: [], can_undo: false }),
  [`${BASE}/pending?status=open`]: jsonResponse({
    subject_id: "historia",
    topic_id: "revolucion-industrial",
    open_count: 3,
    items: [],
  }),
  [`${BASE}/summary`]: jsonResponse({
    subject_id: "historia",
    topic_id: "revolucion-industrial",
    sources: { notes: 2, book: 0, pdf: 0, web: 2 },
    sessions: 1,
    session_minutes: 20,
    open_pending: 3,
    notes_version: 2,
    generated: [],
  }),
  [`${SOURCES}/notes/page-001.jpg/meta`]: meta(`${TOPIC}/sources/notes/page-001.jpg`, "notes", {}, "Gran Bretaña, s. XVIII"),
  [`${SOURCES}/notes/page-002.jpg/meta`]: meta(`${TOPIC}/sources/notes/page-002.jpg`, "notes", {}, "Carbón y hierro"),
  "POST /api/sessions": jsonResponse(SESSION),
  [`${BASE}/workspace/stream`]: () => streamResponse().response,
};

let fakes: CaptureFakes;
const restores: Array<() => void> = [];

beforeEach(() => {
  window.sessionStorage.clear();
  fakes = installCaptureFakes();
  restores.push(swapProperty(HTMLMediaElement.prototype, "play", () => Promise.resolve()));
  restores.push(swapProperty(URL, "createObjectURL", () => "blob:fake"));
  restores.push(swapProperty(URL, "revokeObjectURL", () => undefined));
});

afterEach(() => {
  for (const restore of restores.splice(0).reverse()) restore();
  fakes.restore();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPage(routes: Record<string, Response | (() => Response)> = ROUTES) {
  const fetchMock = stubApi(routes);
  render(<WorkspacePage subjectId="historia" topicId="revolucion-industrial" />, { wrapper: ConfirmProvider });
  return fetchMock;
}

function tab(name: RegExp | string) {
  return screen.getByRole("tab", { name });
}

/** The source's detail over the document column (#473). */
function detail(name: string) {
  const slot = document.querySelector<HTMLElement>(".workspace-detail");
  expect(slot).not.toBeNull();
  return within(slot!).getByRole("dialog", { name });
}

it("shows the two columns: tabs above the chat, the notes on the right, under a compact header", async () => {
  const fetchMock = renderPage();

  // #450: one bar -- Construir · Estudiar, then the topic's name; no doubts counter, no spend.
  const header = screen.getByRole("banner");
  expect(await within(header).findByRole("heading", { level: 1, name: "La Revolución Industrial" })).toBeInTheDocument();
  expect(within(header).getByRole("navigation", { name: "Modo del tema" })).toBeInTheDocument();
  expect(within(header).getByRole("link", { name: "Mesa de estudio" })).toHaveAttribute("href", "/");
  // #534: Recursos is the first tab and the one selected on entry.
  expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Recursos", "Captura"]);
  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
  expect(tab("Captura")).toHaveAttribute("aria-selected", "false");
  const chatRegion = screen.getByRole("region", { name: "Chat" });
  expect(within(chatRegion).getByRole("region", { name: "Chat con el asistente" })).toBeInTheDocument();
  expect(within(chatRegion).queryByRole("heading")).toBeNull();
  expect(within(chatRegion).getByLabelText("Mensaje para el asistente")).toHaveAttribute("placeholder", "Chatea con el asistente…");
  const doc = screen.getByRole("region", { name: "Documento" });
  expect(await within(doc).findByRole("heading", { name: /Contexto/ })).toBeInTheDocument();
  expect(screen.queryByRole("status", { name: "Dudas pendientes" })).toBeNull();
  expect(within(header).queryByText(/dudas pendientes|Este tema:|Esta sesión:/)).toBeNull();
  expect(fetchMock.mock.calls.some(([path]) => String(path).includes("/pending") || String(path).includes("/cost"))).toBe(false);
  // The capture flow is preset to the topic: no subject or topic picker.
  expect(await screen.findByRole("button", { name: "Empezar una sesión nueva", hidden: true })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Asignatura" })).toBeNull();
}, PAGE_TEST_TIMEOUT);

it("moves between the tabs with the arrow keys", async () => {
  renderPage();

  tab("Recursos").focus();
  fireEvent.keyDown(tab("Recursos"), { key: "ArrowRight" });
  expect(tab("Captura")).toHaveAttribute("aria-selected", "true");
  expect(tab("Captura")).toHaveFocus();
  fireEvent.keyDown(tab("Captura"), { key: "ArrowRight" });
  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(tab("Recursos"), { key: "End" });
  expect(tab("Captura")).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(tab("Captura"), { key: "Home" });
  expect(tab("Recursos")).toHaveFocus();
  await screen.findByRole("button", { name: "Empezar una sesión nueva", hidden: true });
}, PAGE_TEST_TIMEOUT);

it("pauses a running capture on Recursos and resumes it back on Captura (#450)", async () => {
  renderPage();

  // Recursos is the tab on entry (#534): go to Captura to start.
  fireEvent.click(tab("Captura"));
  fireEvent.click(await screen.findByRole("button", { name: "Empezar una sesión nueva" }));
  await waitFor(() => expect(fakes.sockets).toHaveLength(1));
  const socket = fakes.sockets[0];
  await act(async () => {
    socket.serverOpen();
    socket.serverMessage(
      JSON.stringify({
        type: "hello.ack",
        protocol_version: PROTOCOL_VERSION,
        stt_mode: "client",
        clock_offset_ms: 0,
        server_time_ms: NOW,
      }),
    );
  });
  await waitFor(() =>
    expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent("La cámara está en marcha."),
  );
  expect(tab("Captura en curso")).toBeInTheDocument();
  // #470: the state is a red recording dot next to «Captura», not the words «en curso».
  const dot = () => tab(/^Captura/).querySelector(".workspace-rec")!;
  expect(dot()).toHaveClass("workspace-rec-on");
  expect(tab(/^Captura/)).not.toHaveTextContent("en curso");
  // The embedded capture (#413): the page's one main is the workspace's, the capture has no h1
  // of its own, no second doubts counter and no «Terminar y preparar apuntes»; since #470 its one
  // button is the camera icon.
  expect(screen.getAllByRole("main")).toHaveLength(1);
  const captureTab = document.getElementById("workspace-panel-capture")!;
  expect(within(captureTab).queryByRole("heading", { level: 1 })).toBeNull();
  expect(screen.queryByRole("status", { name: "Dudas pendientes" })).toBeNull();
  expect(within(captureTab).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual(["Capturar página"]);
  expect(screen.queryByRole("button", { name: /preparar apuntes/ })).toBeNull();

  fireEvent.click(tab("Recursos"));

  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
  const capturePanel = document.getElementById("workspace-panel-capture")!;
  expect(capturePanel).not.toBeVisible();
  // Still mounted and connected, but the camera and the recognizer stop and the backend hears `pause`.
  expect(within(capturePanel).getByRole("button", { name: "Capturar página", hidden: true })).toBeInTheDocument();
  expect(socket.readyState).toBe(WebSocket.OPEN);
  await waitFor(() => expect(fakes.videoTrack.readyState).toBe("ended"));
  expect(fakes.recognitions.some((r) => r.stopCount > 0 || r.abortCount > 0)).toBe(true);
  const buttons = () =>
    socket.sent
      .filter((frame) => frame.kind === "text")
      .map((frame) => JSON.parse(String(frame.text)) as { type: string; button?: string })
      .filter((m) => m.type === "button")
      .map((m) => m.button);
  expect(buttons()).toEqual(["pause"]);
  expect(tab("Captura en pausa")).toBeInTheDocument();
  expect(dot()).not.toHaveClass("workspace-rec-on");
  expect(tab(/^Captura/)).not.toHaveTextContent("en pausa");
  expect(within(capturePanel).getByRole("status", { name: "Captura en pausa", hidden: true })).toHaveTextContent(
    "vuelve a la pestaña Captura",
  );

  fireEvent.click(tab("Captura en pausa"));
  expect(screen.getByRole("button", { name: "Capturar página" })).toBeVisible();
  expect(buttons()).toEqual(["pause", "resume"]);
  await waitFor(() =>
    expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent("La cámara está en marcha."),
  );
  expect(tab("Captura en curso")).toBeInTheDocument();
  await waitFor(() => expect(dot()).toHaveClass("workspace-rec-on"));
  expect(fakes.sockets).toHaveLength(1);
}, PAGE_TEST_TIMEOUT);

it("opens a footnote's source over the document, and closes it with the X or Escape (#473)", async () => {
  renderPage();

  const refs = await screen.findAllByRole("link", { name: "Fuente: Apuntes, página 2" });
  const documentColumn = screen.getByRole("region", { name: "Documento" });
  const heading = within(documentColumn).getByRole("heading", { name: /Contexto/ });
  // #485: the body under the document's pinned header is what scrolls.
  const documentBody = documentColumn.querySelector<HTMLElement>(".workspace-document-body")!;
  expect(documentBody).toContainElement(heading);
  documentBody.scrollTop = 120;
  fireEvent.click(refs[0]);

  // Over the document, not in Recursos: the tabs stay where they were.
  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
  expect(within(document.getElementById("workspace-panel-resources")!).queryByRole("dialog")).toBeNull();
  const dialog = detail("Apuntes, página 2");
  expect(documentColumn).not.toContainElement(dialog);
  expect(await within(dialog).findByText("Carbón y hierro")).toBeInTheDocument();
  expect(within(dialog).getByRole("img", { name: "Apuntes, página 2" })).toHaveAttribute(
    "src",
    `${SOURCES}/notes/page-002.page.jpg`,
  );
  expect(within(dialog).getByRole("heading", { name: "Apuntes, página 2" })).toHaveFocus();

  // The X, named «Cerrar»: closed, the focus back on the footnote, the document untouched.
  fireEvent.click(within(dialog).getByRole("button", { name: "Cerrar" }));
  expect(document.querySelector(".workspace-detail")).toBeNull();
  expect(refs[0]).toHaveFocus();
  expect(heading.isConnected).toBe(true);
  expect(within(documentColumn).getByRole("heading", { name: /Contexto/ })).toBe(heading);
  expect(documentBody.scrollTop).toBe(120);

  // Escape anywhere closes it too.
  fireEvent.click(refs[0]);
  expect(detail("Apuntes, página 2")).toBeInTheDocument();
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
  expect(document.querySelector(".workspace-detail")).toBeNull();
  expect(refs[0]).toHaveFocus();
  expect(heading.isConnected).toBe(true);
}, PAGE_TEST_TIMEOUT);

it("lists the topic's sources in Recursos and opens one in the viewer", async () => {
  renderPage();
  await screen.findByRole("heading", { name: /Contexto/ });

  fireEvent.click(tab("Recursos"));

  const resources = document.getElementById("workspace-panel-resources")!;
  const pages = await within(resources).findByRole("list", { name: "Fuentes del tema" });
  // The list first shows the notes' citations alone; the summary's counts and the sources'
  // metadata arrive on their own, so wait for the whole list.
  await waitFor(
    () =>
      expect(within(pages).getAllByRole("button").filter((b) => b.classList.contains("resource-open")).map((b) => b.querySelector(".resource-title")?.textContent)).toEqual([
        "Página 1 · apuntes",
        "Página 2 · apuntes",
        "Libro, página 1",
        "PDF",
        "Web: 001-maquina-de-vapor.md",
      ]),
    { timeout: 5000 },
  );
  // All five are cited by the notes: each has the «Incorporada a los apuntes» corner badge (#461),
  // and no counts line or log-like note about uncited webs is shown.
  expect(within(pages).getAllByRole("img", { name: "Incorporada a los apuntes" })).toHaveLength(5);
  expect(within(resources).queryByText(/pendientes ·|todavía no citan/)).toBeNull();

  const card = within(pages).getAllByRole("button").find((b) => b.classList.contains("resource-open") && b.textContent === "Página 1 · apuntes")!;
  fireEvent.click(card);
  const dialog = detail("Apuntes, página 1");
  expect(await within(dialog).findByText("Gran Bretaña, s. XVIII")).toBeInTheDocument();
  // The list stays as it was in its box, and the focus goes back to the card on close.
  expect(within(resources).queryByRole("dialog")).toBeNull();
  expect(within(resources).getByRole("list", { name: "Fuentes del tema" })).toBe(pages);
  fireEvent.click(within(dialog).getByRole("button", { name: "Cerrar" }));
  expect(card).toHaveFocus();
}, PAGE_TEST_TIMEOUT);

it("starts with the Recursos/Captura panel collapsed when the document has notes, and the student can toggle it (#534)", async () => {
  renderPage();

  const panel = screen.getByRole("region", { name: "Recursos y captura" });
  await waitFor(() => expect(panel).toHaveAttribute("data-collapsed", "true"));
  // The toggle is an icon at the start of the card, before the tab list.
  const toggle = screen.getByRole("button", { name: "Mostrar recursos y captura" });
  expect(panel.firstElementChild).toBe(toggle);
  expect(toggle).toHaveTextContent("");
  fireEvent.click(toggle);
  expect(panel).not.toHaveAttribute("data-collapsed");
  expect(window.sessionStorage.getItem("studentassistant.workspace.sourcesCollapsed.historia/revolucion-industrial")).toBe("0");
  fireEvent.click(screen.getByRole("button", { name: "Ocultar recursos y captura" }));
  expect(panel).toHaveAttribute("data-collapsed", "true");
  // Choosing a tab shows the panel again.
  fireEvent.click(tab("Captura"));
  expect(panel).not.toHaveAttribute("data-collapsed");
  expect(tab("Captura")).toHaveAttribute("aria-selected", "true");
  await screen.findByRole("button", { name: "Empezar una sesión nueva" });
}, PAGE_TEST_TIMEOUT);

it("starts with the Recursos/Captura panel expanded while the document has not been started (#534)", async () => {
  renderPage({ ...ROUTES, [`${BASE}/notes`]: jsonResponse({ detail: "El tema no tiene apuntes." }, 404) });

  expect(await screen.findByText(EMPTY_NOTES)).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Recursos y captura" })).not.toHaveAttribute("data-collapsed");
  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
});

it("remembers the student's choice of the panel for this topic within the session (#534)", async () => {
  window.sessionStorage.setItem("studentassistant.workspace.sourcesCollapsed.historia/revolucion-industrial", "0");
  renderPage();

  expect(await screen.findByRole("heading", { name: /Contexto/ })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Recursos y captura" })).not.toHaveAttribute("data-collapsed");
});

it("says there are no notes yet instead of an error", async () => {
  renderPage({ ...ROUTES, [`${BASE}/notes`]: jsonResponse({ detail: "El tema no tiene apuntes." }, 404) });

  expect(await screen.findByText(EMPTY_NOTES)).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("refreshes the document after the chat applied a change", async () => {
  let reads = 0;
  const edited = NOTES.replace("La Revolución Industrial empezó", "La Revolución Industrial, dicen, empezó");
  const stream = streamResponse();
  const fetchMock = renderPage({
    ...ROUTES,
    [`${BASE}/notes`]: () => (reads++ === 0 ? notes() : notes(edited, { revision: "b".repeat(64) })),
    [`${BASE}/workspace/stream`]: () => stream.response,
    [`POST ${BASE}/workspace/messages`]: () =>
      jsonResponse(
        {
          message_id: "msg-0123abcd",
          requests: [{ request_id: "req-t1", kind: "edit", summary: "Ampliar", text: "Amplía", detector: "typed" }],
          classified: true,
        },
        202,
      ),
  });
  await screen.findByRole("heading", { name: /Contexto/ });

  const chat = screen.getByRole("region", { name: "Chat" });
  fireEvent.change(within(chat).getByLabelText("Mensaje para el asistente"), { target: { value: "Amplía" } });
  fireEvent.click(within(chat).getByRole("button", { name: "Enviar" }));
  expect(await within(chat).findByText("En cola…")).toBeInTheDocument();

  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-t1", origin: "typed", kind: "revise" }));
    stream.push(sseEvent("turn.result", { ...revision({ message: "Amplía" }), turn_id: "turn-1", request_id: "req-t1", kind: "revise" }));
    stream.push(sseEvent("notes.changed", { revision: "b".repeat(64), origin: "editor", summary: "Ampliado", turn_id: "turn-1" }));
  });

  expect(await screen.findByText(/dicen, empezó/)).toBeInTheDocument();
  expect(screen.getByText(/dicen, empezó/).closest(".notes-block")).toHaveClass("notes-changed");
  expect(fetchMock.mock.calls.filter(([path]) => path === `${BASE}/notes`)).toHaveLength(2);
}, PAGE_TEST_TIMEOUT);

it("opens a contradiction's source of a chat doubt over the document", async () => {
  const stream = streamResponse();
  renderPage({
    ...ROUTES,
    [`${BASE}/workspace/stream`]: () => stream.response,
  });
  await screen.findByRole("heading", { name: /Contexto/ });

  act(() =>
    stream.push(
      sseEvent("doubt.asked", {
        pending_id: "p-000007",
        question: "¿Empezó en 1760 o en 1780?",
        suggestions: [],
        options: [
          { source_id: "sources/notes/page-002.jpg", says: "1760" },
          { source_id: "sources/book/page-001.jpg", says: "1780" },
        ],
        refs: ["sources/notes/page-002.jpg", "sources/book/page-001.jpg"],
      }),
    ),
  );
  fireEvent.click(await screen.findByRole("button", { name: "Ver la fuente: página 2" }, { timeout: 5000 }));
  const dialog = detail("Apuntes, página 2");
  expect(await within(dialog).findByText("Carbón y hierro")).toBeInTheDocument();
}, PAGE_TEST_TIMEOUT);

it("switches the single column between document, capture/resources and chat", async () => {
  renderPage();
  const root = screen.getByRole("banner").closest(".workspace")!;
  const group = screen.getByRole("group", { name: "Qué mostrar" });

  expect(root).toHaveAttribute("data-view", "document");
  expect(within(group).getByRole("button", { name: "Documento" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(within(group).getByRole("button", { name: "Chat" }));
  expect(root).toHaveAttribute("data-view", "chat");
  expect(within(group).getByRole("button", { name: "Chat" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(within(group).getByRole("button", { name: "Recursos/Captura" }));
  expect(root).toHaveAttribute("data-view", "left");
  // A footnote's source takes the document view's place (#473); the document stays mounted.
  fireEvent.click(within(group).getByRole("button", { name: "Documento" }));
  const refs = await screen.findAllByRole("link", { name: "Fuente: Apuntes, página 1" });
  fireEvent.click(refs[0]);
  expect(root).toHaveAttribute("data-view", "document");
  expect(root).toHaveAttribute("data-detail", "open");
  expect(detail("Apuntes, página 1")).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Documento" })).toBeInTheDocument();
  fireEvent.click(within(detail("Apuntes, página 1")).getByRole("button", { name: "Cerrar" }));
  expect(root).not.toHaveAttribute("data-detail");
  expect(root).toHaveAttribute("data-view", "document");

  // Opened from Recursos, it shows in the document's place; closing it goes back to Recursos.
  fireEvent.click(within(group).getByRole("button", { name: "Recursos/Captura" }));
  fireEvent.click(tab("Recursos"));
  const pages = await within(document.getElementById("workspace-panel-resources")!).findByRole("list", { name: "Fuentes del tema" });
  const card = within(pages).getAllByRole("button").find((b) => b.classList.contains("resource-open"))!;
  fireEvent.click(card);
  expect(root).toHaveAttribute("data-view", "document");
  fireEvent.keyDown(document.body, { key: "Escape" });
  expect(root).toHaveAttribute("data-view", "left");
  expect(card).toHaveFocus();
  // The capture tab is never unmounted by the switch.
  expect(document.getElementById("workspace-panel-capture")).not.toBeNull();
}, PAGE_TEST_TIMEOUT);

it("lists every stored source from the topic's source list, uncited webs included (#323)", async () => {
  const fetchMock = renderPage({
    ...ROUTES,
    [`${BASE}/sources`]: jsonResponse({
      subject_id: "historia",
      topic_id: "revolucion-industrial",
      sources: [
        { vault_id: `${TOPIC}/sources/notes/page-001.jpg`, kind: "notes", title: null },
        { vault_id: `${TOPIC}/sources/notes/page-003.jpg`, kind: "notes", title: null },
        { vault_id: `${TOPIC}/sources/web/001-maquina-de-vapor.md`, kind: "web", title: "La máquina de vapor" },
        { vault_id: `${TOPIC}/sources/web/002-telar.md`, kind: "web", title: "El telar mecánico" },
      ],
    }),
    [`${SOURCES}/web/002-telar.md`]: new Response("# El telar\n\nTexto de la web.", {
      status: 200,
      headers: { "Content-Type": "text/markdown; charset=utf-8" },
    }),
    [`${SOURCES}/web/002-telar.md/meta`]: jsonResponse({
      vault_id: `${TOPIC}/sources/web/002-telar.md`,
      kind: "web",
      media_type: "text/markdown; charset=utf-8",
      size: 10,
      meta: { title: "El telar mecánico", url: "https://example.org/telar" },
      transcription: null,
    }),
  });
  await screen.findByRole("heading", { name: /Contexto/ });

  fireEvent.click(tab("Recursos"));

  const resources = document.getElementById("workspace-panel-resources")!;
  const cards = await within(resources).findByRole("list", { name: "Fuentes del tema" });
  // By kind: the real files (page 3, not a guessed page 2) before what only the notes cite, and
  // the uncited web too. The list first shows the notes' citations alone: wait for the listing.
  await waitFor(
    () =>
      expect(within(cards).getAllByRole("button").filter((b) => b.classList.contains("resource-open")).map((b) => b.querySelector(".resource-title")?.textContent)).toEqual([
        "Página 1 · apuntes",
        "Página 3 · apuntes",
        "Página 2 · apuntes",
        "Libro, página 1",
        "PDF",
        "Web: La máquina de vapor",
        "Web: El telar mecánico",
      ]),
    { timeout: 5000 },
  );
  expect(within(resources).queryByText(/todavía no citan/)).toBeNull();
  expect(fetchMock.mock.calls.some(([path]) => path === `${BASE}/summary`)).toBe(false);

  fireEvent.click(within(cards).getByRole("button", { name: /^(Web: )?El telar mecánico/ }));
  const dialog = detail("Web: El telar mecánico");
  expect(await within(dialog).findByText(/Texto de la web/)).toBeInTheDocument();
});

it("builds the resource list from the listed sources and the notes' citations", () => {
  const list = resourceList(
    [
      { vault_id: `${TOPIC}/sources/book/page-083.jpg`, kind: "book" },
      { vault_id: `${TOPIC}/sources/pdf/page-001.pdf`, kind: "pdf", title: "Tema 1.pdf" },
      { vault_id: `${TOPIC}/sources/pdf/page-002.pdf`, kind: "pdf", title: null },
      { vault_id: `${TOPIC}/sources/images/img-001.png`, kind: "images" },
      { vault_id: `${TOPIC}/sources/other/x.bin`, kind: "other" },
    ],
    parseNotes(NOTES),
  );
  const byKind = Object.fromEntries(list.groups.map((g) => [g.kind, g.items.map((i) => i.title)]));
  expect(byKind.book).toEqual(["Libro, página 83", "Libro, página 1"]);
  expect(byKind.pdf).toEqual(["Tema 1.pdf", "PDF 2", "PDF, página 3"]);
  expect(byKind.web).toEqual(["Web: 001-maquina-de-vapor.md"]);
  expect(byKind.images).toEqual(["Imagen pegada 1"]);
  expect(Object.keys(byKind)).not.toContain("other");
  expect(list.groups.find((g) => g.kind === "pdf")!.items[0].definition).toBe("[Tema 1.pdf](../sources/pdf/page-001.pdf)");
  expect(list.uncitedWebs).toBe(0);
  expect(resourceList([], null)).toEqual({ groups: [], uncitedWebs: 0 });
});

it("builds the resource list from the counts and the notes' citations", () => {
  const list = resourceList({ notes: 1, book: 1, pdf: 1, web: 1 }, parseNotes(NOTES));
  const byKind = Object.fromEntries(list.groups.map((g) => [g.kind, g.items.map((i) => i.title)]));
  expect(byKind.notes).toEqual(["Apuntes, página 1", "Apuntes, página 2"]);
  expect(byKind.book).toEqual(["Libro, página 1"]);
  expect(byKind.pdf).toEqual(["PDF 1", "PDF, página 3"]);
  expect(byKind.web).toEqual(["Web: 001-maquina-de-vapor.md"]);
  expect(byKind.transcript).toHaveLength(2);
  expect(resourceList(null, parseNotes("# T\n\nUna imagen.[^img001]\n\n[^img001]: [Imagen pegada 1](../sources/images/img-001.png)\n")).groups).toEqual([
    {
      kind: "images",
      title: "Imágenes",
      items: [{ key: "images/img-001.png", label: "img001", definition: "[Imagen pegada 1](../sources/images/img-001.png)", title: "Imagen pegada 1" }],
    },
  ]);
  expect(list.uncitedWebs).toBe(0);
  expect(resourceList(null, null)).toEqual({ groups: [], uncitedWebs: 0 });
});

it("links the notes' versions from the header, naming the current version", async () => {
  renderPage();

  const header = screen.getByRole("banner");
  const link = await within(header).findByRole("link", { name: "Versiones (actual: v2)" });
  expect(link).toHaveAttribute("href", "/subjects/historia/topics/revolucion-industrial/versions");
  expect(link).toHaveTextContent("Versiones · v2");
});

it("marks the open doubts in the notes and shows one in the chat from its badge or «Siguiente duda.» (#516)", async () => {
  const marks = (asked: boolean) =>
    jsonResponse({
      subject: "historia",
      topic: "revolucion-industrial",
      count: 2,
      marks: [
        { pending_id: "d-1", kind: "illegible", text: "Palabra dudosa", level: "block", blocks: [{ section: "contexto", number: 1 }], section: null, asked },
        { pending_id: "d-2", kind: "incomplete", text: "Falta algo", level: "section", blocks: [], section: "causas", asked: false },
      ],
    });
  let asked = false;
  let readsAfterAsk = 0;
  const fetchMock = renderPage({
    ...ROUTES,
    [`${BASE}/doubts/marks`]: () => {
      if (asked) readsAfterAsk += 1;
      return marks(asked);
    },
    [`POST ${BASE}/doubts/d-1/ask`]: () => {
      asked = true;
      return jsonResponse({ pending_id: "d-1", asked: true, status: "open", summary: null });
    },
    [`POST ${BASE}/doubts/d-2/ask`]: () => jsonResponse({ detail: "Esa duda ya está cerrada.", code: "doubt_closed" }, 409),
  });
  const doc = screen.getByRole("region", { name: "Documento" });
  const badge = await within(doc).findByRole("button", { name: "1 duda abierta en este párrafo: verla en el chat" });
  expect(badge.closest(".notes-block")).toHaveTextContent(/empezó en Gran Bretaña/);
  expect(within(doc).getByRole("button", { name: "1 duda abierta sobre esta sección: verla en el chat" }).closest(".notes-block")).toHaveTextContent(
    /Causas/,
  );
  const chatRegion = screen.getByRole("region", { name: "Chat" });
  expect(within(chatRegion).getByText("Tienes 2 dudas marcadas en los apuntes")).toBeInTheDocument();

  fireEvent.click(badge);
  const posted = () => fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST").map(([path]) => path);
  await waitFor(() => expect(posted()).toEqual([`${BASE}/doubts/d-1/ask`]));
  // Asked: the marks are read again (d-1 now asked).
  await waitFor(() => expect(readsAfterAsk).toBeGreaterThan(0));
  await new Promise((resolve) => setTimeout(resolve, 0));

  // «Siguiente duda.» goes in order, skipping the doubt already asked; a refusal is explained.
  fireEvent.click(within(chatRegion).getByRole("button", { name: "Siguiente duda." }));
  await waitFor(() => expect(posted()).toEqual([`${BASE}/doubts/d-1/ask`, `${BASE}/doubts/d-2/ask`]));
  expect(await within(chatRegion).findByRole("alert")).toHaveTextContent("No se pudo mostrar la duda: Esa duda ya está cerrada.");
}, PAGE_TEST_TIMEOUT);
