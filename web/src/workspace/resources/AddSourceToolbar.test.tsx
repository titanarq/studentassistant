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
      onOpen={() => undefined}
    />,
  );
}

function listCalls(fetchMock: ReturnType<typeof stubApi>) {
  return fetchMock.mock.calls.filter(([path, init]) => path === `${BASE}/sources` && (init?.method ?? "GET") === "GET").length;
}

/** The toolbar under the list (#461). */
function toolbar() {
  return within(screen.getByRole("group", { name: "Añadir fuentes" }));
}

it("is a bottom icon toolbar after the list, with Spanish names and tooltips, and no «Añadir fuente» panel", async () => {
  stubApi({ [`${BASE}/sources`]: sourceList([PAGE], [PAGE]).route, [`${BASE}/book`]: jsonResponse({ title: null }) });
  renderTab();
  const kept = await screen.findByRole("list", { name: "Fuentes del tema" });

  const group = screen.getByRole("group", { name: "Añadir fuentes" });
  // After the list in the DOM, outside it.
  expect(kept.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(kept).not.toContainElement(group);
  expect(toolbar().getAllByRole("button").map((b) => [b.getAttribute("aria-label"), b.getAttribute("title")])).toEqual([
    ["Añadir una página web", "Añadir una página web (URL)"],
    ["Subir archivos PDF", "Subir archivos PDF"],
    ["Libro de texto", "Título del libro de texto"],
  ]);
  expect(screen.queryByRole("button", { name: "Añadir fuente" })).toBeNull();
  expect(screen.queryByRole("form")).toBeNull();

  // The web popover opens with the address focused; Escape closes it and gives the focus back.
  fireEvent.click(toolbar().getByRole("button", { name: "Añadir una página web" }));
  expect(toolbar().getByRole("button", { name: "Añadir una página web" })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByLabelText("Dirección de la página")).toHaveFocus();
  fireEvent.keyDown(screen.getByLabelText("Dirección de la página"), { key: "Escape" });
  expect(screen.queryByRole("form")).toBeNull();
  expect(toolbar().getByRole("button", { name: "Añadir una página web" })).toHaveFocus();

  // The book popover shows the topic's book form; pressing its button again closes it.
  fireEvent.click(toolbar().getByRole("button", { name: "Libro de texto" }));
  expect(screen.getByRole("form", { name: "Libro de texto" })).toBeInTheDocument();
  expect(await screen.findByText("Este tema aún no tiene libro de texto.")).toBeInTheDocument();
  fireEvent.click(toolbar().getByRole("button", { name: "Libro de texto" }));
  expect(screen.queryByRole("form")).toBeNull();
});

it("uploads the chosen PDFs one by one, re-reads the list once and logs nothing", async () => {
  const list = sourceList([PAGE], [PAGE, PDF]);
  const fetchMock = stubApi({
    [`${BASE}/sources`]: list.route,
    [`POST ${BASE}/sources/pdf`]: () => {
      list.state.added = true;
      return jsonResponse(IMPORTED, 201);
    },
  });
  renderTab();
  await screen.findByRole("button", { name: /^Página 1/ });
  expect(listCalls(fetchMock)).toBe(1);

  const files = [
    new File(["%PDF-1.7"], "Tema 4.pdf", { type: "application/pdf" }),
    new File(["%PDF-1.7"], "Tema 5.pdf", { type: "application/pdf" }),
  ];
  const input = screen.getByTestId("resources-upload-input");
  expect(input).toHaveAttribute("multiple");
  fireEvent.change(input, { target: { files } });

  await waitFor(() => expect(listCalls(fetchMock)).toBe(2));
  const posted = fetchMock.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([, init]) => ((init!.body as FormData).get("file") as File).name);
  expect(posted).toEqual(["Tema 4.pdf", "Tema 5.pdf"]);
  const kept = await screen.findByRole("list", { name: "Fuentes del tema" });
  await waitFor(() => expect(within(kept).getAllByRole("button").filter((b) => b.classList.contains("resource-open"))).toHaveLength(2));
  await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  expect(screen.queryByText(/añadido/)).toBeNull();
});

it("adds a web page by URL: the popover closes, the list is read again and the page appears", async () => {
  const list = sourceList([PAGE], [PAGE, WEB]);
  const fetchMock = stubApi({
    [`${BASE}/sources`]: list.route,
    [`POST ${BASE}/web-pages`]: () => {
      list.state.added = true;
      return jsonResponse(ADDED, 201);
    },
  });
  renderTab();
  await screen.findByRole("button", { name: /^Página 1/ });

  fireEvent.click(toolbar().getByRole("button", { name: "Añadir una página web" }));
  fireEvent.change(screen.getByLabelText("Dirección de la página"), { target: { value: ADDED.url } });
  fireEvent.click(screen.getByRole("button", { name: "Añadir" }));

  await waitFor(() => expect(listCalls(fetchMock)).toBe(2));
  expect(screen.queryByRole("form")).toBeNull();
  expect(await screen.findByRole("button", { name: /^Web: La Bastilla/ })).toBeInTheDocument();
  expect(screen.queryByText(/guardada como fuente/)).toBeNull();
});

it("saves the book title from its popover and closes it", async () => {
  const fetchMock = stubApi({
    [`${BASE}/sources`]: sourceList([PAGE], [PAGE]).route,
    [`GET ${BASE}/book`]: jsonResponse({ title: null }),
    [`PUT ${BASE}/book`]: jsonResponse({ title: "Historia 4.º ESO" }),
  });
  renderTab();
  await screen.findByRole("button", { name: /^Página 1/ });

  fireEvent.click(toolbar().getByRole("button", { name: "Libro de texto" }));
  await screen.findByText("Este tema aún no tiene libro de texto.");
  fireEvent.change(screen.getByLabelText("Título del libro"), { target: { value: "Historia 4.º ESO" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));

  await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
  await waitFor(() => expect(listCalls(fetchMock)).toBe(2));
  const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
  expect(JSON.parse(put![1]!.body as string)).toEqual({ title: "Historia 4.º ESO" });
});

it("keeps a refusal in Spanish in the popover and does not re-read", async () => {
  const fetchMock = stubApi({
    [`${BASE}/sources`]: sourceList([PAGE], [PAGE]).route,
    [`POST ${BASE}/sources/pdf`]: jsonResponse({ detail: "El PDF pesa demasiado (máximo 50 MB)." }, 413),
    [`POST ${BASE}/web-pages`]: jsonResponse({ detail: "No se pudo descargar la página." }, 502),
  });
  renderTab();
  await screen.findByRole("button", { name: /^Página 1/ });

  const file = new File(["%PDF-1.7"], "Enorme.pdf", { type: "application/pdf" });
  fireEvent.change(screen.getByTestId("resources-upload-input"), { target: { files: [file] } });
  expect(await screen.findByRole("alert")).toHaveTextContent("No se ha añadido «Enorme.pdf»: El PDF pesa demasiado (máximo 50 MB).");
  fireEvent.click(screen.getByRole("button", { name: "Cerrar" }));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(toolbar().getByRole("button", { name: "Subir archivos PDF" })).toHaveFocus();

  fireEvent.click(toolbar().getByRole("button", { name: "Añadir una página web" }));
  fireEvent.change(screen.getByLabelText("Dirección de la página"), { target: { value: "no es una url" } });
  fireEvent.click(screen.getByRole("button", { name: "Añadir" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/Pega la dirección completa de la página/);
  fireEvent.change(screen.getByLabelText("Dirección de la página"), { target: { value: ADDED.url } });
  fireEvent.click(screen.getByRole("button", { name: "Añadir" }));
  expect(await screen.findByText("No se ha guardado la página: No se pudo descargar la página.")).toBeInTheDocument();
  expect(screen.getByRole("form", { name: "Añadir una página web" })).toBeInTheDocument();

  expect(listCalls(fetchMock)).toBe(1);
});
