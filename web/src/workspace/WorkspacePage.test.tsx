import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installCaptureFakes, swapProperty, type CaptureFakes } from "../capture/testing";
import { revision } from "../chat/testChat";
import { NOTES } from "../notes/testNotes";
import { PROTOCOL_VERSION } from "../protocol";
import { jsonResponse, sseEvent, streamResponse, stubApi } from "../test/mockApi";
import { resourceList } from "./resources";
import WorkspacePage, { DOUBTS_TOOLTIP, EMPTY_NOTES } from "./WorkspacePage";
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
  render(<WorkspacePage subjectId="historia" topicId="revolucion-industrial" />);
  return fetchMock;
}

function tab(name: RegExp | string) {
  return screen.getByRole("tab", { name });
}

it("shows the two columns: tabs above the chat, the notes on the right, and the doubts counter", async () => {
  renderPage();

  expect(screen.getByRole("heading", { name: "Espacio de estudio" })).toBeInTheDocument();
  expect(tab("Captura")).toHaveAttribute("aria-selected", "true");
  expect(tab("Recursos")).toHaveAttribute("aria-selected", "false");
  expect(within(screen.getByRole("region", { name: "Chat" })).getByRole("heading", { name: "Chat con el asistente" })).toBeInTheDocument();
  const doc = screen.getByRole("region", { name: "Documento" });
  expect(await within(doc).findByRole("heading", { name: /Contexto/ })).toBeInTheDocument();
  // Since #413 the counter is plain text: the doubts are asked in the chat, not on /pending.
  const counter = screen.getByRole("status", { name: "Dudas pendientes" });
  await waitFor(() => expect(counter).toHaveTextContent("3 dudas pendientes"));
  expect(within(counter).queryByRole("link")).toBeNull();
  expect(counter).toHaveAttribute("title", DOUBTS_TOOLTIP);
  expect(screen.queryByRole("link", { name: /dudas pendientes/ })).toBeNull();
  // The capture flow is preset to the topic: no subject or topic picker.
  expect(await screen.findByRole("button", { name: "Empezar una sesión nueva" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Asignatura" })).toBeNull();
}, PAGE_TEST_TIMEOUT);

it("moves between the tabs with the arrow keys", async () => {
  renderPage();

  tab("Captura").focus();
  fireEvent.keyDown(tab("Captura"), { key: "ArrowRight" });
  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
  expect(tab("Recursos")).toHaveFocus();
  fireEvent.keyDown(tab("Recursos"), { key: "ArrowRight" });
  expect(tab("Captura")).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(tab("Captura"), { key: "End" });
  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(tab("Recursos"), { key: "Home" });
  expect(tab("Captura")).toHaveFocus();
  await screen.findByRole("button", { name: "Empezar una sesión nueva" });
}, PAGE_TEST_TIMEOUT);

it("keeps a running capture mounted when switching to Recursos, and marks it as running", async () => {
  renderPage();

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
  // The embedded capture (#413): the page's one main is the workspace's, the capture has no h1
  // of its own, no second doubts counter and no «Terminar y preparar apuntes».
  expect(screen.getAllByRole("main")).toHaveLength(1);
  const captureTab = document.getElementById("workspace-panel-capture")!;
  expect(within(captureTab).queryByRole("heading", { level: 1 })).toBeNull();
  expect(screen.getAllByRole("status", { name: "Dudas pendientes" })).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Terminar" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /preparar apuntes/ })).toBeNull();

  fireEvent.click(tab("Recursos"));

  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
  const capturePanel = document.getElementById("workspace-panel-capture")!;
  expect(capturePanel).not.toBeVisible();
  expect(within(capturePanel).getByRole("button", { name: "Capturar", hidden: true })).toBeInTheDocument();
  expect(socket.readyState).toBe(WebSocket.OPEN);
  expect(fakes.videoTrack.readyState).toBe("live");
  expect(fakes.recognitions.every((r) => r.stopCount === 0 && r.abortCount === 0)).toBe(true);
  expect(tab("Captura en curso")).toBeInTheDocument();

  fireEvent.click(tab("Captura en curso"));
  expect(screen.getByRole("button", { name: "Capturar" })).toBeVisible();
  expect(fakes.sockets).toHaveLength(1);
}, PAGE_TEST_TIMEOUT);

it("opens a footnote's source in Recursos instead of a panel over the document", async () => {
  renderPage();

  const refs = await screen.findAllByRole("link", { name: "Fuente: Apuntes, página 2" });
  fireEvent.click(refs[0]);

  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
  const resources = document.getElementById("workspace-panel-resources")!;
  const dialog = within(resources).getByRole("dialog", { name: "Apuntes, página 2" });
  expect(await within(dialog).findByText("Carbón y hierro")).toBeInTheDocument();

  fireEvent.click(within(dialog).getByRole("button", { name: "Cerrar" }));
  expect(within(resources).queryByRole("dialog")).toBeNull();
  expect(await within(resources).findByRole("list", { name: "Fuentes del tema" })).toBeInTheDocument();
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
      expect(within(pages).getAllByRole("button").map((b) => b.querySelector(".resource-title")?.textContent)).toEqual([
        "Página 1 · apuntes",
        "Página 2 · apuntes",
        "Libro, página 1",
        "PDF",
        "Web: 001-maquina-de-vapor.md",
      ]),
    { timeout: 5000 },
  );
  expect(await within(resources).findByText("0 pendientes · 5 incorporadas · 0 apartadas", {}, { timeout: 5000 })).toBeInTheDocument();
  expect(
    await within(resources).findByText("Hay 1 web guardada que los apuntes todavía no citan.", {}, { timeout: 5000 }),
  ).toBeInTheDocument();

  fireEvent.click(within(pages).getByRole("button", { name: /Página 1 · apuntes/ }));
  const dialog = within(resources).getByRole("dialog", { name: "Apuntes, página 1" });
  expect(await within(dialog).findByText("Gran Bretaña, s. XVIII")).toBeInTheDocument();
}, PAGE_TEST_TIMEOUT);

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

it("reads the doubts counter again on a doubt of the chat and opens a contradiction's source in Recursos", async () => {
  // The counter is also read when the notes load, so it drops to 2 only once the doubt is asked:
  // "2 dudas pendientes" then proves the read the doubt triggered.
  let asked = false;
  const stream = streamResponse();
  const fetchMock = renderPage({
    ...ROUTES,
    [`${BASE}/workspace/stream`]: () => stream.response,
    [`${BASE}/pending?status=open`]: () =>
      jsonResponse({ subject_id: "historia", topic_id: "revolucion-industrial", open_count: asked ? 2 : 3, items: [] }),
  });
  await screen.findByRole("heading", { name: /Contexto/ });
  await waitFor(() => expect(screen.getByRole("status", { name: "Dudas pendientes" })).toHaveTextContent("3 dudas pendientes"));
  const readsBefore = fetchMock.mock.calls.filter(([path]) => path === `${BASE}/pending?status=open`).length;

  asked = true;
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
  // The counter and the chat's doubt card render from independent paths: await each on its own.
  await waitFor(() => expect(screen.getByRole("status", { name: "Dudas pendientes" })).toHaveTextContent("2 dudas pendientes"), {
    timeout: 5000,
  });
  expect(fetchMock.mock.calls.filter(([path]) => path === `${BASE}/pending?status=open`).length).toBeGreaterThan(readsBefore);

  fireEvent.click(await screen.findByRole("button", { name: "Ver la fuente: página 2" }, { timeout: 5000 }));
  expect(tab("Recursos")).toHaveAttribute("aria-selected", "true");
  const resources = document.getElementById("workspace-panel-resources")!;
  const dialog = within(resources).getByRole("dialog", { name: "Apuntes, página 2" });
  expect(await within(dialog).findByText("Carbón y hierro")).toBeInTheDocument();
}, PAGE_TEST_TIMEOUT);

it("switches the single column between document, capture/resources and chat", async () => {
  renderPage();
  const root = screen.getByRole("heading", { name: "Espacio de estudio" }).closest(".workspace")!;
  const group = screen.getByRole("group", { name: "Qué mostrar" });

  expect(root).toHaveAttribute("data-view", "document");
  expect(within(group).getByRole("button", { name: "Documento" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(within(group).getByRole("button", { name: "Chat" }));
  expect(root).toHaveAttribute("data-view", "chat");
  expect(within(group).getByRole("button", { name: "Chat" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(within(group).getByRole("button", { name: "Captura/Recursos" }));
  expect(root).toHaveAttribute("data-view", "left");
  // A footnote switches the narrow view to the sources.
  fireEvent.click(within(group).getByRole("button", { name: "Documento" }));
  const refs = await screen.findAllByRole("link", { name: "Fuente: Apuntes, página 1" });
  fireEvent.click(refs[0]);
  expect(root).toHaveAttribute("data-view", "left");
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
      expect(within(cards).getAllByRole("button").map((b) => b.querySelector(".resource-title")?.textContent)).toEqual([
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

  fireEvent.click(within(cards).getByRole("button", { name: /El telar mecánico/ }));
  const dialog = within(resources).getByRole("dialog", { name: "Web: El telar mecánico" });
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
      title: "Imágenes pegadas",
      items: [{ key: "images/img-001.png", label: "img001", definition: "[Imagen pegada 1](../sources/images/img-001.png)", title: "Imagen pegada 1" }],
    },
  ]);
  expect(list.uncitedWebs).toBe(0);
  expect(resourceList(null, null)).toEqual({ groups: [], uncitedWebs: 0 });
});

function costStatus(overrides: Record<string, unknown> = {}) {
  return jsonResponse({
    session_usd: 0,
    day_usd: 0,
    max_usd_per_session: null,
    max_usd_per_day: null,
    observer_paused: false,
    editor_needs_confirmation: false,
    unpriced_session_calls: 0,
    unpriced_day_calls: 0,
    unpriced_models: [],
    ...overrides,
  });
}

function topicCost(usd: number) {
  const totals = { usd, tokens: 10, input_tokens: 5, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0, calls: 1, unpriced_calls: 0 };
  return jsonResponse({ subject_id: "historia", topic_id: "revolucion-industrial", total: totals, sessions: [], no_session: totals });
}

it("links the notes' versions from the header, naming the current version", async () => {
  renderPage();

  const header = screen.getByRole("banner");
  const link = await within(header).findByRole("link", { name: "Versiones (actual: v2)" });
  expect(link).toHaveAttribute("href", "/subjects/historia/topics/revolucion-industrial/versions");
  expect(link).toHaveTextContent("Versiones");
});

it("shows the topic's spend in the header and reads it again after the notes changed", async () => {
  let costs = 0;
  let reads = 0;
  const stream = streamResponse();
  renderPage({
    ...ROUTES,
    [`${BASE}/notes`]: () => (reads++ === 0 ? notes() : notes(NOTES, { revision: "c".repeat(64), version: 3 })),
    [`${BASE}/workspace/stream`]: () => stream.response,
    [`${BASE}/cost`]: () => topicCost(costs++ === 0 ? 0.01 : 0.05),
    "/api/cost": costStatus(),
  });
  const header = screen.getByRole("banner");
  await within(header).findByRole("link", { name: "Versiones (actual: v2)" });
  expect(await within(header).findByText(/^Este tema: /)).toBeInTheDocument();

  act(() => {
    stream.push(sseEvent("notes.changed", { revision: "c".repeat(64), origin: "editor", summary: "Ampliado", turn_id: null }));
  });

  // The cost and the notes reload independently after notes.changed; await each on its own.
  expect(await within(header).findByText("Este tema: 0,0500 USD", {}, { timeout: 5000 })).toBeInTheDocument();
  expect(
    await within(header).findByRole("link", { name: "Versiones (actual: v3)" }, { timeout: 5000 }),
  ).toBeInTheDocument();
});

it("shows the open session's spend when the topic has a session open", async () => {
  renderPage({
    ...ROUTES,
    "/api/subjects/historia/topics": jsonResponse({
      subject_id: "historia",
      topics: [
        {
          topic_id: "revolucion-industrial",
          subject_id: "historia",
          name: "La Revolución Industrial",
          open_session_id: "s-20260926-1000",
        },
      ],
    }),
    "/api/cost?subject=historia&topic=revolucion-industrial&session=s-20260926-1000": costStatus({ session_usd: 0.25 }),
  });

  expect(await within(screen.getByRole("banner")).findByText("Esta sesión: 0,2500 USD")).toBeInTheDocument();
});
