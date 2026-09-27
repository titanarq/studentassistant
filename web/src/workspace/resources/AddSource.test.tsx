import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { parseNotes } from "../../notes/markdown";
import { jsonResponse, stubApi } from "../../test/mockApi";
import ResourcesTab from "../ResourcesTab";

const TOPIC = "subjects/historia/topics/revolucion-industrial";
const BASE = "/api/subjects/historia/topics/revolucion-industrial";
const TREE = parseNotes("# La Revolución Industrial\n\nTexto.\n");

const PAGE = { vault_id: `${TOPIC}/sources/notes/page-001.jpg`, kind: "notes", title: null };
const PDF = { vault_id: `${TOPIC}/sources/pdf/page-001.pdf`, kind: "pdf", title: "Tema 4" };
const WEB = { vault_id: `${TOPIC}/sources/web/001-la-bastilla.md`, kind: "web", title: "La Bastilla" };

const IMPORTED = {
  subject_id: "historia",
  topic_id: "revolucion-industrial",
  source_id: "sources/pdf/page-001.pdf",
  vault_id: PDF.vault_id,
  original_name: "Tema 4.pdf",
  original_page_count: 120,
  first_page: 1,
  last_page: 120,
  page_count: 120,
  pages_without_text: [],
};

const ADDED = {
  source_id: "sources/web/001-la-bastilla.md",
  vault_id: WEB.vault_id,
  title: "La Bastilla",
  url: "https://historia.example.edu/bastilla",
  already_kept: false,
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** The topic's source list: `before` until something is added, then `after`. */
function sourceList(before: object[], after: object[]) {
  const state = { added: false };
  const route = () =>
    jsonResponse({ subject_id: "historia", topic_id: "revolucion-industrial", sources: state.added ? after : before });
  return { state, route };
}

function renderTab() {
  render(
    <ResourcesTab
      subjectId="historia"
      topicId="revolucion-industrial"
      tree={TREE}
      refreshKey={0}
      open={null}
      onOpen={() => undefined}
      onClose={() => undefined}
    />,
  );
}

function listCalls(fetchMock: ReturnType<typeof stubApi>) {
  return fetchMock.mock.calls.filter(([path, init]) => path === `${BASE}/sources` && (init?.method ?? "GET") === "GET").length;
}

function openChoice(name: string) {
  fireEvent.click(screen.getByRole("button", { name: "Añadir fuente" }));
  fireEvent.click(within(screen.getByRole("group", { name: "Tipo de fuente" })).getByRole("button", { name }));
}

it("opens three choices, each showing its form, and Cancelar closes the panel", async () => {
  stubApi({ [`${BASE}/sources`]: sourceList([PAGE], [PAGE]).route, [`${BASE}/book`]: jsonResponse({ title: null }) });
  renderTab();
  await screen.findByRole("button", { name: /Página 1/ });

  fireEvent.click(screen.getByRole("button", { name: "Añadir fuente" }));
  const choices = within(screen.getByRole("group", { name: "Tipo de fuente" }));
  expect(choices.getAllByRole("button").map((button) => button.textContent)).toEqual([
    "PDF",
    "Página web",
    "Libro de texto",
    "Cancelar",
  ]);
  expect(screen.queryByRole("form")).toBeNull();

  fireEvent.click(choices.getByRole("button", { name: "PDF" }));
  expect(screen.getByRole("form", { name: "Añadir un PDF" })).toBeInTheDocument();
  expect(choices.getByRole("button", { name: "PDF" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(choices.getByRole("button", { name: "Página web" }));
  expect(screen.getByRole("form", { name: "Añadir una página web" })).toBeInTheDocument();
  expect(screen.queryByRole("form", { name: "Añadir un PDF" })).toBeNull();
  fireEvent.click(choices.getByRole("button", { name: "Libro de texto" }));
  expect(screen.getByRole("form", { name: "Libro de texto" })).toBeInTheDocument();
  expect(await screen.findByText("Este tema aún no tiene libro de texto.")).toBeInTheDocument();

  fireEvent.click(choices.getByRole("button", { name: "Cancelar" }));
  expect(screen.queryByRole("group", { name: "Tipo de fuente" })).toBeNull();
  expect(screen.queryByRole("form")).toBeNull();
  expect(screen.getByRole("button", { name: "Añadir fuente" })).toBeInTheDocument();
});

it("a PDF upload closes the panel, re-reads the list and the PDF appears", async () => {
  const list = sourceList([PAGE], [PAGE, PDF]);
  const fetchMock = stubApi({
    [`${BASE}/sources`]: list.route,
    [`POST ${BASE}/sources/pdf`]: () => {
      list.state.added = true;
      return jsonResponse(IMPORTED, 201);
    },
  });
  renderTab();
  await screen.findByRole("button", { name: /Página 1/ });
  expect(listCalls(fetchMock)).toBe(1);

  openChoice("PDF");
  const file = new File(["%PDF-1.7"], "Tema 4.pdf", { type: "application/pdf" });
  fireEvent.change(screen.getByLabelText("Archivo PDF"), { target: { files: [file] } });
  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));

  expect(await screen.findByText("PDF «Tema 4.pdf» añadido.")).toBeInTheDocument();
  expect(screen.queryByRole("form")).toBeNull();
  await waitFor(() => expect(listCalls(fetchMock)).toBe(2));
  const kept = await screen.findByRole("list", { name: "Fuentes del tema" });
  await waitFor(() => expect(within(kept).getAllByRole("button")).toHaveLength(2));
});

it("a web page by URL closes the panel, re-reads the list and the page appears", async () => {
  const list = sourceList([PAGE], [PAGE, WEB]);
  const fetchMock = stubApi({
    [`${BASE}/sources`]: list.route,
    [`POST ${BASE}/web-pages`]: () => {
      list.state.added = true;
      return jsonResponse(ADDED, 201);
    },
  });
  renderTab();
  await screen.findByRole("button", { name: /Página 1/ });

  openChoice("Página web");
  fireEvent.change(screen.getByLabelText("Dirección de la página"), { target: { value: ADDED.url } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar como fuente" }));

  expect(await screen.findByText("Página «La Bastilla» guardada como fuente.")).toBeInTheDocument();
  expect(screen.queryByRole("form")).toBeNull();
  await waitFor(() => expect(listCalls(fetchMock)).toBe(2));
  expect(await screen.findByRole("button", { name: /La Bastilla/ })).toBeInTheDocument();
});

it("a book title closes the panel and re-reads the list", async () => {
  const fetchMock = stubApi({
    [`${BASE}/sources`]: sourceList([PAGE], [PAGE]).route,
    [`GET ${BASE}/book`]: jsonResponse({ title: null }),
    [`PUT ${BASE}/book`]: jsonResponse({ title: "Historia 4.º ESO" }),
  });
  renderTab();
  await screen.findByRole("button", { name: /Página 1/ });

  openChoice("Libro de texto");
  await screen.findByText("Este tema aún no tiene libro de texto.");
  fireEvent.change(screen.getByLabelText("Título del libro"), { target: { value: "Historia 4.º ESO" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));

  expect(await screen.findByText("Libro de texto: «Historia 4.º ESO».")).toBeInTheDocument();
  expect(screen.queryByRole("form")).toBeNull();
  await waitFor(() => expect(listCalls(fetchMock)).toBe(2));
  const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
  expect(JSON.parse(put![1]!.body as string)).toEqual({ title: "Historia 4.º ESO" });
});

it("a refused add keeps the panel open with the form's Spanish error and does not re-read", async () => {
  const fetchMock = stubApi({
    [`${BASE}/sources`]: sourceList([PAGE], [PAGE]).route,
    [`POST ${BASE}/sources/pdf`]: jsonResponse({ detail: "El PDF pesa demasiado (máximo 50 MB)." }, 413),
    [`POST ${BASE}/web-pages`]: jsonResponse({ detail: "No se pudo descargar la página." }, 502),
  });
  renderTab();
  await screen.findByRole("button", { name: /Página 1/ });

  openChoice("PDF");
  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Elige primero un PDF.");
  const file = new File(["%PDF-1.7"], "Enorme.pdf", { type: "application/pdf" });
  fireEvent.change(screen.getByLabelText("Archivo PDF"), { target: { files: [file] } });
  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));
  expect(await screen.findByText("No se ha añadido el PDF: El PDF pesa demasiado (máximo 50 MB).")).toBeInTheDocument();
  expect(screen.getByRole("form", { name: "Añadir un PDF" })).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Página web" }));
  fireEvent.change(screen.getByLabelText("Dirección de la página"), { target: { value: "no es una url" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar como fuente" }));
  expect(await screen.findByText(/Pega la dirección completa de la página/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Dirección de la página"), { target: { value: ADDED.url } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar como fuente" }));
  expect(await screen.findByText(/^No se ha guardado la página:/)).toBeInTheDocument();
  expect(screen.getByRole("form", { name: "Añadir una página web" })).toBeInTheDocument();

  expect(listCalls(fetchMock)).toBe(1);
});
