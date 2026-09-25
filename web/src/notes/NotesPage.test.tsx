import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import NotesPage from "./NotesPage";
import { NOTES } from "./testNotes";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const TOPIC = "subjects/historia/topics/revolucion-industrial";
const SOURCES = `/api/sources/${TOPIC}/sources`;

const TOPICS = jsonResponse({
  subject_id: "historia",
  topics: [{ topic_id: "revolucion-industrial", subject_id: "historia", name: "La Revolución Industrial" }],
});

function notes(text = NOTES) {
  return jsonResponse({ subject_id: "historia", topic_id: "revolucion-industrial", text, version: 2 });
}

function meta(vaultId: string, kind: string, sidecar: Record<string, unknown> | null, transcription: string | null = null) {
  return jsonResponse({ vault_id: vaultId, kind, media_type: "image/jpeg", size: 10, meta: sidecar, transcription });
}

function text(body: string) {
  return new Response(body, { status: 200, headers: { "Content-Type": "text/plain; charset=utf-8" } });
}

const ROUTES = {
  "/api/subjects/historia/topics": TOPICS,
  "/api/subjects/historia/topics/revolucion-industrial/notes": notes(),
  "/api/subjects/historia/topics/revolucion-industrial/notes/chat": jsonResponse({ turns: [], can_undo: false }),
  [`${SOURCES}/notes/page-001.jpg/meta`]: meta(`${TOPIC}/sources/notes/page-001.jpg`, "notes", { session_t_ms: 1 }),
  [`${SOURCES}/notes/page-001.md`]: text("# Revolución Industrial\n- Gran Bretaña, s. XVIII"),
  [`${SOURCES}/notes/page-002.jpg/meta`]: meta(`${TOPIC}/sources/notes/page-002.jpg`, "notes", {}, "Carbón y hierro"),
  [`${SOURCES}/pdf/page-001.pdf/meta`]: meta(`${TOPIC}/sources/pdf/page-001.pdf`, "pdf", {
    original_name: "Tema 4.pdf",
    first_page: 82,
    last_page: 94,
    page_count: 13,
  }),
  [`${SOURCES}/pdf/page-001.p003.txt`]: text("James Watt patentó en 1769 su máquina."),
  [`${SOURCES}/web/001-maquina-de-vapor.md/meta`]: meta(`${TOPIC}/sources/web/001-maquina-de-vapor.md`, "web", {
    url: "https://es.wikipedia.org/wiki/M%C3%A1quina_de_vapor",
    fetched_at: "2026-09-24T18:00:00Z",
  }),
  [`${SOURCES}/web/001-maquina-de-vapor.md`]: text("La máquina de vapor es un motor de combustión externa."),
  "/api/sessions/20260924-183000/transcript?subject=historia&topic=revolucion-industrial&t=00%3A02%3A34-00%3A03%3A10":
    jsonResponse({
      session_id: "20260924-183000",
      subject_id: "historia",
      topic_id: "revolucion-industrial",
      ref: "sessions/20260924-183000#t=00:02:34-00:03:10",
      start_ms: 154000,
      end_ms: 190000,
      segments: [
        { seq: 7, t_start: 154000, t_end: 170000, text: "Esto empieza en Inglaterra" },
        { seq: 8, t_start: 170500, t_end: 189000, text: "hacia mil setecientos cincuenta" },
      ],
    }),
};

function renderPage(routes: Record<string, Response> = ROUTES) {
  const fetchMock = stubApi(routes);
  render(<NotesPage subjectId="historia" topicId="revolucion-industrial" />);
  return fetchMock;
}

async function openRef(name: string, index = 0) {
  const refs = await screen.findAllByRole("link", { name });
  fireEvent.click(refs[index]);
  return refs[index];
}

it("renders the notes with anchored headings, footnotes and the AI content highlighted", async () => {
  renderPage();

  expect(await screen.findByRole("heading", { level: 1, name: "La Revolución Industrial" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "← Tema La Revolución Industrial" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-industrial",
  );
  expect(screen.getByText(/versión 2/)).toBeInTheDocument();
  expect(screen.getByRole("heading", { level: 2, name: /2\. Causas/ })).toHaveAttribute("id", "causas");
  expect(screen.getByRole("link", { name: "Enlace a la sección causas" })).toHaveAttribute("href", "#causas");
  expect(screen.getByRole("table")).toHaveTextContent("Industria textil");
  expect(screen.getAllByRole("listitem").some((li) => li.textContent?.includes("Disponibilidad de carbón"))).toBe(true);

  const refs = screen.getAllByRole("link", { name: "Fuente: Apuntes, página 1" });
  expect(refs).toHaveLength(2);
  expect(refs[0]).toHaveTextContent("[1]");
  expect(refs[0]).toHaveAttribute("href", "#fn-p1");

  const ia = screen.getByText(/movimiento obrero/);
  expect(ia).toHaveClass("notes-ia");
  expect(within(ia).getByRole("link", { name: "Ampliado por la IA" })).toHaveTextContent("[IA]");
  expect(screen.getByText(/Watt mejoró/)).not.toHaveClass("notes-ia");

  const footnotes = screen.getByRole("region", { name: "Fuentes" });
  expect(within(footnotes).getByRole("link", { name: "PDF, página 3" })).toBeInTheDocument();
});

it("opens a handwritten page in the side panel: zoomable image and transcription", async () => {
  renderPage();
  await openRef("Fuente: Apuntes, página 1");

  const panel = await screen.findByRole("dialog", { name: "Apuntes, página 1" });
  const image = within(panel).getByRole("img", { name: "Apuntes, página 1" });
  expect(image).toHaveAttribute("src", `${SOURCES}/notes/page-001.page.jpg`);
  fireEvent.error(image);
  expect(image).toHaveAttribute("src", `${SOURCES}/notes/page-001.jpg`);
  expect(await within(panel).findByText(/Gran Bretaña, s\. XVIII/)).toBeInTheDocument();

  expect(within(panel).getByLabelText("Zoom")).toHaveTextContent("100 %");
  fireEvent.click(within(panel).getByRole("button", { name: "Acercar" }));
  expect(within(panel).getByLabelText("Zoom")).toHaveTextContent("125 %");
  expect(image).toHaveStyle({ width: "125%" });
  const frame = within(panel).getByRole("region", { name: /Imagen de la página/ });
  fireEvent.keyDown(frame, { key: "+" });
  expect(within(panel).getByLabelText("Zoom")).toHaveTextContent("150 %");
  fireEvent.keyDown(frame, { key: "0" });
  expect(within(panel).getByLabelText("Zoom")).toHaveTextContent("100 %");
});

it("prefers the sidecar's transcription of a page", async () => {
  renderPage();
  await openRef("Fuente: Apuntes, página 2");

  const panel = await screen.findByRole("dialog", { name: "Apuntes, página 2" });
  expect(await within(panel).findByText("Carbón y hierro")).toBeInTheDocument();
});

it("says so when a page has no transcription yet", async () => {
  renderPage({
    ...ROUTES,
    [`${SOURCES}/notes/page-001.md`]: jsonResponse({ detail: "No existe esa fuente en la bóveda." }, 404),
  });
  await openRef("Fuente: Apuntes, página 1");

  expect(await screen.findByText("Esta página todavía no está transcrita.")).toBeInTheDocument();
});

it("keeps the reading position and gives the focus back when the panel closes", async () => {
  const scrollTo = vi.fn();
  vi.stubGlobal("scrollTo", scrollTo);
  renderPage();
  const article = await screen.findByRole("article", { name: "Apuntes" });
  const ref = await openRef("Fuente: Apuntes, página 1", 1);

  const panel = await screen.findByRole("dialog", { name: "Apuntes, página 1" });
  expect(within(panel).getByRole("heading", { name: "Apuntes, página 1" })).toHaveFocus();
  expect(ref).toHaveAttribute("aria-current", "true");
  expect(screen.getByRole("article", { name: "Apuntes" })).toBe(article);

  fireEvent.keyDown(panel, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(ref).toHaveFocus();
  expect(screen.getByRole("article", { name: "Apuntes" })).toBe(article);
  expect(scrollTo).not.toHaveBeenCalled();

  await openRef("Fuente: Apuntes, página 1", 1);
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cerrar" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(ref).toHaveFocus();
});

it("shows a PDF page with its number in the original PDF", async () => {
  renderPage();
  await openRef("Fuente: PDF, página 3");

  const panel = await screen.findByRole("dialog", { name: "PDF, página 3" });
  expect(await within(panel).findByText("PDF «Tema 4.pdf», página 84")).toBeInTheDocument();
  expect(within(panel).getByRole("img")).toHaveAttribute("src", `${SOURCES}/pdf/page-001.p003.jpg`);
  expect(within(panel).getByRole("link", { name: "Abrir el PDF" })).toHaveAttribute(
    "href",
    `${SOURCES}/pdf/page-001.pdf#page=3`,
  );
  expect(await within(panel).findByText("James Watt patentó en 1769 su máquina.")).toBeInTheDocument();
});

it("shows a transcript excerpt with its timestamps", async () => {
  renderPage();
  await openRef("Fuente: Transcripción, 00:02:34–00:03:10");

  const panel = await screen.findByRole("dialog", { name: "Transcripción, 00:02:34–00:03:10" });
  const excerpt = await within(panel).findByRole("list", { name: "Fragmento de la transcripción" });
  const lines = within(excerpt).getAllByRole("listitem");
  expect(lines[0]).toHaveTextContent("02:34 Esto empieza en Inglaterra");
  expect(lines[1]).toHaveTextContent("02:50 hacia mil setecientos cincuenta");
});

it("shows a web snapshot, marked as an external source, with the URL it was taken from", async () => {
  renderPage();
  const [ref] = await screen.findAllByRole("link", { name: "Fuente externa (web): Web: 001-maquina-de-vapor.md" });
  expect(ref.closest("sup")).toHaveClass("notes-ref-web");
  const footnotes = screen.getByRole("region", { name: "Fuentes" });
  expect(within(footnotes).getByText(/fuente externa/)).toBeInTheDocument();
  await openRef("Fuente externa (web): Web: 001-maquina-de-vapor.md");

  const panel = await screen.findByRole("dialog");
  expect(await within(panel).findByText(/Fuente externa: copia de/)).toBeInTheDocument();
  expect(await within(panel).findByText(/motor de combustión externa/)).toBeInTheDocument();
  expect(await within(panel).findByRole("link", { name: /wikipedia/ })).toHaveAttribute(
    "href",
    "https://es.wikipedia.org/wiki/M%C3%A1quina_de_vapor",
  );
});

it("explains the AI mark and switches sources without closing the panel", async () => {
  renderPage();
  await openRef("Ampliado por la IA");

  expect(await screen.findByRole("dialog", { name: "Ampliado por la IA" })).toHaveTextContent(
    "no está en tus fuentes",
  );
  await openRef("Fuente: Apuntes, página 2");
  expect(await screen.findByRole("dialog", { name: "Apuntes, página 2" })).toBeInTheDocument();
});

it("opens the panel from the footnote list too", async () => {
  renderPage();
  const footnotes = await screen.findByRole("region", { name: "Fuentes" });
  fireEvent.click(within(footnotes).getByRole("link", { name: "Libro, página 1" }));

  const panel = await screen.findByRole("dialog", { name: "Libro, página 1" });
  expect(within(panel).getByRole("img")).toHaveAttribute("src", `${SOURCES}/book/page-001.page.jpg`);
});

it("reports a source the vault does not have", async () => {
  renderPage({ ...ROUTES, [`${SOURCES}/pdf/page-001.pdf/meta`]: jsonResponse({ detail: "No existe esa fuente en la bóveda." }, 404) });
  await openRef("Fuente: PDF, página 3");

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar el PDF: No existe esa fuente en la bóveda.");
});

it("says there are no notes yet", async () => {
  renderPage({
    "/api/subjects/historia/topics": TOPICS,
    "/api/subjects/historia/topics/revolucion-industrial/notes": jsonResponse(
      { detail: "Todavía no hay apuntes de este tema." },
      404,
    ),
  });

  expect(await screen.findByText("Todavía no hay apuntes de este tema.")).toBeInTheDocument();
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
});

it("never renders the notes' HTML as markup", async () => {
  renderPage({
    ...ROUTES,
    "/api/subjects/historia/topics/revolucion-industrial/notes": notes(
      '# T\n\n<img src=x onerror="alert(1)"> [clic](javascript:alert(1))[^p1]\n\n[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n',
    ),
  });

  const paragraph = await screen.findByText(/<img src=x/);
  expect(paragraph.querySelector("img")).toBeNull();
  expect(screen.queryByRole("link", { name: "clic" })).not.toBeInTheDocument();
});
