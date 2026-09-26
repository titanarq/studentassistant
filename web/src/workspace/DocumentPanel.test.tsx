import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { parseNotes } from "../notes/markdown";
import { UPLOADING } from "../noteEditor/NoteEditor";
import { jsonResponse, stubApi } from "../test/mockApi";
import DocumentPanel, { ASSISTANT_CHANGED, BUSY, CHANGED_WHILE_EDITING } from "./DocumentPanel";
import { type WorkspaceNotes, type WorkspaceState, WorkspaceContext } from "./state";

const BASE = "/api/subjects/lengua/topics/la-comunicacion";
const R1 = "1".repeat(64);
const R2 = "2".repeat(64);
const R3 = "3".repeat(64);

const TEXT = `# La comunicación

## Funciones {#funciones}

Cada función se centra en un elemento.[^p3]

Truco para recordarlas.[^est]

[^p3]: [Apuntes, página 3](../sources/notes/page-003.jpg)
[^est]: Escrito por el estudiante
`;

const THEIRS = TEXT.replace("Cada función", "Cada una de las funciones");

function workspace(notes: WorkspaceNotes, reloadNotes = vi.fn(async () => undefined)): WorkspaceState {
  return { subjectId: "lengua", topicId: "la-comunicacion", notes, changedSections: new Set(), reloadNotes };
}

const ready = (text = TEXT, revision = R1): WorkspaceNotes => ({ kind: "ready", text, revision, version: 4 });

function renderPanel(state: WorkspaceState) {
  const tree = state.notes.kind === "ready" ? parseNotes(state.notes.text) : null;
  const view = (value: WorkspaceState) => (
    <WorkspaceContext.Provider value={value}>
      <DocumentPanel topicName="La comunicación" tree={tree} onOpenSource={() => undefined} activeLabel={null} />
    </WorkspaceContext.Provider>
  );
  const result = render(view(state));
  return { ...result, update: (next: WorkspaceState) => result.rerender(view(next)) };
}

/** Enters the editor and switches it to the raw Markdown. */
async function editAsMarkdown(): Promise<HTMLTextAreaElement> {
  fireEvent.click(screen.getByRole("button", { name: "Editar" }));
  fireEvent.click(await screen.findByRole("button", { name: "Markdown" }));
  return screen.getByLabelText("Apuntes en Markdown") as HTMLTextAreaElement;
}

function putBodies(fetchMock: ReturnType<typeof stubApi>) {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method === "PUT")
    .map(([, init]) => JSON.parse(String(init?.body)) as { text: string; base_revision: string | null });
}

const saved = (text: string, revision: string) =>
  jsonResponse({ revision, commit: "abc", diff: "", notes: text, changed_sections: ["funciones"], normalised: false, notes_changed: true });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("opens the visual editor with fixed provenance chips and saves an untouched document unchanged", async () => {
  const fetchMock = stubApi({ [`PUT ${BASE}/notes`]: saved(TEXT, R1) });
  const reloadNotes = vi.fn(async () => undefined);
  renderPanel(workspace(ready(), reloadNotes));

  expect(screen.getByText("v4 · guardado")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Editar" }));
  expect(await screen.findByText("Editando sobre v4")).toBeInTheDocument();
  const surface = screen.getByLabelText("Apuntes en edición");
  await waitFor(() => expect(within(surface).getByLabelText("Escrito por ti")).toHaveTextContent("tú"));
  expect(within(surface).getByLabelText("Fuente p3")).toHaveAttribute("contenteditable", "false");
  await waitFor(() => expect(screen.getByRole("button", { name: "Negrita" })).toBeEnabled());

  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  await waitFor(() => expect(reloadNotes).toHaveBeenCalledWith(["funciones"]));
  expect(putBodies(fetchMock)).toEqual([{ text: TEXT, base_revision: R1 }]);
  expect(screen.queryByRole("button", { name: "Guardar" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Editar" })).toBeInTheDocument();
});

it("saves the edited Markdown with the revision the edit started from", async () => {
  const fetchMock = stubApi({ [`PUT ${BASE}/notes`]: saved(THEIRS, R2) });
  renderPanel(workspace(ready()));
  const area = await editAsMarkdown();
  expect(area.value).toBe(TEXT);
  fireEvent.change(area, { target: { value: THEIRS } });
  expect(screen.getByLabelText("Vista previa")).toHaveTextContent("Cada una de las funciones");

  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  await waitFor(() => expect(putBodies(fetchMock)).toEqual([{ text: THEIRS, base_revision: R1 }]));
});

it("on notes_changed shows both versions and retries on the current one keeping the student's text", async () => {
  const mine = TEXT.replace("Truco para recordarlas.", "Truco: cada función mira a un elemento.");
  let conflict = true;
  const fetchMock = stubApi({
    [`PUT ${BASE}/notes`]: () =>
      conflict
        ? jsonResponse({ detail: "Los apuntes han cambiado.", code: "notes_changed", text: THEIRS, revision: R2 }, 409)
        : saved(mine, R3),
  });
  const reloadNotes = vi.fn(async () => undefined);
  renderPanel(workspace(ready(), reloadNotes));
  const area = await editAsMarkdown();
  fireEvent.change(area, { target: { value: mine } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));

  const alert = await screen.findByRole("alert", { name: CHANGED_WHILE_EDITING });
  expect(within(alert).getByLabelText("Tu versión")).toHaveTextContent("Truco: cada función mira a un elemento.");
  expect(within(alert).getByLabelText("Versión actual")).toHaveTextContent("Cada una de las funciones");
  expect(screen.getByLabelText("Apuntes en Markdown")).toHaveValue(mine);

  conflict = false;
  fireEvent.click(within(alert).getByRole("button", { name: "Reintentar sobre la versión actual" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Apuntes en Markdown")).toHaveValue(mine);
  expect(reloadNotes).toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  await waitFor(() =>
    expect(putBodies(fetchMock)).toEqual([
      { text: mine, base_revision: R1 },
      { text: mine, base_revision: R2 },
    ]),
  );
});

it("on notes_changed can discard the student's changes", async () => {
  stubApi({ [`PUT ${BASE}/notes`]: jsonResponse({ detail: "x", code: "notes_changed", text: THEIRS, revision: R2 }, 409) });
  const reloadNotes = vi.fn(async () => undefined);
  renderPanel(workspace(ready(), reloadNotes));
  const area = await editAsMarkdown();
  fireEvent.change(area, { target: { value: `${TEXT}\nMás.\n` } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  fireEvent.click(await screen.findByRole("button", { name: "Descartar mis cambios" }));
  await waitFor(() => expect(reloadNotes).toHaveBeenCalled());
  expect(screen.queryByLabelText("Apuntes en Markdown")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Editar" })).toBeInTheDocument();
});

it("on notes_busy asks to wait and keeps the editor", async () => {
  stubApi({ [`PUT ${BASE}/notes`]: jsonResponse({ detail: "Ocupado.", code: "notes_busy" }, 409) });
  renderPanel(workspace(ready()));
  await editAsMarkdown();
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(BUSY);
  expect(screen.getByLabelText("Apuntes en Markdown")).toHaveValue(TEXT);
});

it("on 422 lists the Spanish format errors", async () => {
  stubApi({
    [`PUT ${BASE}/notes`]: jsonResponse(
      { detail: "Los apuntes no cumplen el formato: ...", errors: ["El ancla #funciones está repetida.", "La nota [^p9] no está definida."] },
      422,
    ),
  });
  renderPanel(workspace(ready()));
  await editAsMarkdown();
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  const alert = await screen.findByRole("alert");
  expect(within(alert).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
    "El ancla #funciones está repetida.",
    "La nota [^p9] no está definida.",
  ]);
});

it("uploads a pasted image and inserts its Markdown at the cursor, with a placeholder meanwhile", async () => {
  let answer!: (response: Response) => void;
  const fetchMock = stubApi({
    [`POST ${BASE}/sources/images`]: () =>
      new Promise<Response>((resolve) => {
        answer = resolve;
      }),
  });
  renderPanel(workspace(ready()));
  const area = await editAsMarkdown();
  const at = TEXT.indexOf("Truco");
  area.setSelectionRange(at, at);
  const file = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "captura.png", { type: "image/png" });
  fireEvent.paste(area, { clipboardData: { files: [file], items: [], types: ["Files"] } });

  expect(await screen.findByText(UPLOADING)).toBeInTheDocument();
  const [, init] = fetchMock.mock.calls.find(([path]) => path === `${BASE}/sources/images`) ?? [];
  expect((init?.body as FormData).get("file")).toBeInstanceOf(File);
  await act(async () =>
    answer(
      jsonResponse(
        { source_id: "sources/images/img-001.png", path: "x", markdown: "![Imagen pegada 1](../sources/images/img-001.png)" },
        201,
      ),
    ),
  );
  await waitFor(() => expect(screen.queryByText(UPLOADING)).not.toBeInTheDocument());
  expect(area.value).toBe(`${TEXT.slice(0, at)}![Imagen pegada 1](../sources/images/img-001.png)\n\n${TEXT.slice(at)}`);
  const image = within(screen.getByLabelText("Vista previa")).getByRole("img", { name: "Imagen pegada 1" });
  expect(image).toHaveAttribute("src", "/api/sources/subjects/lengua/topics/la-comunicacion/sources/images/img-001.png");
});

it("shows a Spanish error and inserts nothing when the image upload fails", async () => {
  stubApi({ [`POST ${BASE}/sources/images`]: jsonResponse({ detail: "La imagen supera el máximo (10.0 MB)." }, 413) });
  renderPanel(workspace(ready()));
  const area = await editAsMarkdown();
  const file = new File([new Uint8Array([1, 2, 3])], "grande.png", { type: "image/png" });
  fireEvent.drop(area, { dataTransfer: { files: [file], items: [], types: ["Files"] } });
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo subir la imagen: La imagen supera el máximo (10.0 MB).");
  expect(area.value).toBe(TEXT);
});

it("announces a newer revision while editing without touching the student's text", async () => {
  stubApi({});
  const { update } = renderPanel(workspace(ready()));
  const area = await editAsMarkdown();
  fireEvent.change(area, { target: { value: `${TEXT}\nMío.\n` } });
  expect(screen.queryByText(new RegExp(ASSISTANT_CHANGED))).not.toBeInTheDocument();

  update(workspace(ready(THEIRS, R2)));
  expect(screen.getByRole("status")).toHaveTextContent(ASSISTANT_CHANGED);
  expect(screen.getByLabelText("Apuntes en Markdown")).toHaveValue(`${TEXT}\nMío.\n`);
});

it("starts a document from the topic's title when there are no notes", async () => {
  const fetchMock = stubApi({ [`PUT ${BASE}/notes`]: saved("# La comunicación\n", R1) });
  renderPanel(workspace({ kind: "empty" }));
  const area = await editAsMarkdown();
  expect(area).toHaveValue("# La comunicación\n");
  expect(screen.getByText("Apuntes nuevos")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  await waitFor(() => expect(putBodies(fetchMock)).toEqual([{ text: "# La comunicación\n", base_revision: null }]));
});

it("inserts a table skeleton in the Markdown mode", async () => {
  stubApi({});
  renderPanel(workspace(ready()));
  const area = await editAsMarkdown();
  area.setSelectionRange(area.value.length, area.value.length);
  fireEvent.click(screen.getByRole("button", { name: "Tabla" }));
  expect(area.value).toBe(`${TEXT}\n| Columna 1 | Columna 2 |\n| --- | --- |\n|  |  |\n`);
});
