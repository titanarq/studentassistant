import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installCaptureFakes, swapProperty, type CaptureFakes } from "../capture/testing";
import { revision } from "../chat/testChat";
import { NOTES } from "../notes/testNotes";
import { PROTOCOL_VERSION } from "../protocol";
import { jsonResponse, sseResponse, stubApi } from "../test/mockApi";
import { resourceList } from "./resources";
import WorkspacePage, { EMPTY_NOTES } from "./WorkspacePage";
import { parseNotes } from "../notes/markdown";

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
  expect(within(screen.getByRole("region", { name: "Chat" })).getByRole("heading", { name: "Hablar con el editor" })).toBeInTheDocument();
  const doc = screen.getByRole("region", { name: "Documento" });
  expect(await within(doc).findByRole("heading", { name: /Contexto/ })).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "3 dudas pendientes" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-industrial/pending",
  );
  // The capture flow is preset to the topic: no subject or topic picker.
  expect(await screen.findByRole("button", { name: "Empezar una sesión nueva" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Asignatura" })).toBeNull();
});

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
});

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
});

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
  expect(await within(resources).findByRole("region", { name: "Páginas de apuntes" })).toBeInTheDocument();
});

it("lists the topic's sources in Recursos and opens one in the viewer", async () => {
  renderPage();
  await screen.findByRole("heading", { name: /Contexto/ });

  fireEvent.click(tab("Recursos"));

  const resources = document.getElementById("workspace-panel-resources")!;
  const pages = await within(resources).findByRole("region", { name: "Páginas de apuntes" });
  expect(within(pages).getAllByRole("button").map((b) => b.textContent)).toEqual([
    "Apuntes, página 1",
    "Apuntes, página 2",
  ]);
  expect(within(resources).getByRole("region", { name: "Webs" })).toBeInTheDocument();
  expect(within(resources).getByText("Hay 1 web guardada que los apuntes todavía no citan.")).toBeInTheDocument();

  fireEvent.click(within(pages).getByRole("button", { name: "Apuntes, página 1" }));
  const dialog = within(resources).getByRole("dialog", { name: "Apuntes, página 1" });
  expect(await within(dialog).findByText("Gran Bretaña, s. XVIII")).toBeInTheDocument();
});

it("says there are no notes yet instead of an error", async () => {
  renderPage({ ...ROUTES, [`${BASE}/notes`]: jsonResponse({ detail: "El tema no tiene apuntes." }, 404) });

  expect(await screen.findByText(EMPTY_NOTES)).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("refreshes the document after the chat applied a change", async () => {
  let reads = 0;
  const edited = NOTES.replace("La Revolución Industrial empezó", "La Revolución Industrial, dicen, empezó");
  const fetchMock = renderPage({
    ...ROUTES,
    [`${BASE}/notes`]: () => (reads++ === 0 ? notes() : notes(edited, { revision: "b".repeat(64) })),
    [`POST ${BASE}/notes/chat`]: () => sseResponse([["result", revision()]]),
  });
  await screen.findByRole("heading", { name: /Contexto/ });

  const chat = screen.getByRole("region", { name: "Chat" });
  fireEvent.change(within(chat).getByLabelText("Mensaje para el editor"), { target: { value: "Amplía" } });
  fireEvent.click(within(chat).getByRole("button", { name: "Enviar" }));

  expect(await screen.findByText(/dicen, empezó/)).toBeInTheDocument();
  expect(screen.getByText(/dicen, empezó/).closest(".notes-block")).toHaveClass("notes-changed");
  expect(fetchMock.mock.calls.filter(([path]) => path === `${BASE}/notes`)).toHaveLength(2);
});

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
});

it("builds the resource list from the counts and the notes' citations", () => {
  const list = resourceList({ notes: 1, book: 1, pdf: 1, web: 1 }, parseNotes(NOTES));
  const byKind = Object.fromEntries(list.groups.map((g) => [g.kind, g.items.map((i) => i.title)]));
  expect(byKind.notes).toEqual(["Apuntes, página 1", "Apuntes, página 2"]);
  expect(byKind.book).toEqual(["Libro, página 1"]);
  expect(byKind.pdf).toEqual(["PDF 1", "PDF, página 3"]);
  expect(byKind.web).toEqual(["Web: 001-maquina-de-vapor.md"]);
  expect(byKind.transcript).toHaveLength(2);
  expect(list.uncitedWebs).toBe(0);
  expect(resourceList(null, null)).toEqual({ groups: [], uncitedWebs: 0 });
});
