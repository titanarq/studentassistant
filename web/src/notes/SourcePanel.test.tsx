import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import SourcePanel from "./SourcePanel";
import { TRANSCRIPTION_EDIT_UNSUPPORTED } from "./api";

const TOPIC = "subjects/historia/topics/revolucion-industrial";
const PAGE = `/api/sources/${TOPIC}/sources/notes/page-002.jpg`;
const MD = `/api/sources/${TOPIC}/sources/notes/page-002.md`;
const DEFINITION = "[Apuntes, página 2](../sources/notes/page-002.jpg)";

function meta(transcription: string | null) {
  return jsonResponse({ vault_id: `${TOPIC}/sources/notes/page-002.jpg`, kind: "notes", media_type: "image/jpeg", size: 10, meta: {}, transcription });
}

function markdown(text: string) {
  return new Response(text, { status: 200, headers: { "Content-Type": "text/markdown; charset=utf-8" } });
}

const ROUTES = {
  [`${PAGE}/meta`]: meta("Carbón y hierro"),
  [MD]: markdown("Carbón y hierro"),
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPanel(variant: "panel" | "overlay" = "overlay", onClose = vi.fn()) {
  render(
    <SourcePanel
      subjectId="historia"
      topicId="revolucion-industrial"
      label="p2"
      definition={DEFINITION}
      onClose={onClose}
      variant={variant}
    />,
  );
  return { onClose, dialog: screen.getByRole("dialog", { name: "Apuntes, página 2" }) };
}

const edit = () => screen.getByRole("button", { name: "Editar la transcripción" });
const area = () => screen.getByRole("textbox", { name: "Corrige la transcripción" });

it("shows the capture large and its transcription, with an X named «Cerrar»", async () => {
  stubApi(ROUTES);
  const { dialog, onClose } = renderPanel();
  expect(within(dialog).getByRole("img", { name: "Apuntes, página 2" })).toHaveAttribute("src", `/api/sources/${TOPIC}/sources/notes/page-002.page.jpg`);
  expect(await within(dialog).findByText("Carbón y hierro")).toBeInTheDocument();
  const close = within(dialog).getByRole("button", { name: "Cerrar" });
  expect(close).toHaveClass("source-panel-close");
  expect(close).not.toHaveTextContent("Cerrar");
  fireEvent.click(close);
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("closes with Escape wherever the focus is, but not while a control took the key", async () => {
  stubApi(ROUTES);
  const { onClose } = renderPanel();
  fireEvent.keyDown(document.body, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);

  // Escape in the transcription editor leaves the edit only; the detail stays open.
  fireEvent.click(await screen.findByRole("button", { name: "Editar la transcripción" }));
  fireEvent.keyDown(area(), { key: "Escape" });
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(onClose).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(edit()).toHaveFocus());
});

it("saves a hand-corrected transcription through the route", async () => {
  const fetchMock = stubApi({
    ...ROUTES,
    [`PUT ${PAGE}/transcription`]: () =>
      jsonResponse({ source_path: `${TOPIC}/sources/notes/page-002.jpg`, transcription_path: `${TOPIC}/sources/notes/page-002.md`, text: "Carbón, hierro y vapor\n" }),
  });
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Editar la transcripción" }));
  expect(area()).toHaveValue("Carbón y hierro");
  expect(area()).toHaveFocus();
  fireEvent.change(area(), { target: { value: "Carbón, hierro y vapor" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));

  expect(await screen.findByText("Transcripción guardada.")).toHaveAttribute("role", "status");
  expect(screen.getByText("Carbón, hierro y vapor", { exact: false })).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).toBeNull();
  const call = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(call[0]).toBe(`${PAGE}/transcription`);
  expect(JSON.parse(String(call[1]!.body))).toEqual({ text: "Carbón, hierro y vapor" });
});

it("explains in Spanish why a correction was not saved, and keeps the draft", async () => {
  stubApi({
    ...ROUTES,
    [`PUT ${PAGE}/transcription`]: () => jsonResponse({ detail: "Esta página todavía no tiene transcripción." }, 409),
  });
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Editar la transcripción" }));
  fireEvent.change(area(), { target: { value: "Otra cosa" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo guardar: Esta página todavía no tiene transcripción.");
  expect(area()).toHaveValue("Otra cosa");

  // An empty text is refused before asking the server.
  fireEvent.change(area(), { target: { value: "   " } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  expect(screen.getByRole("alert")).toHaveTextContent("La transcripción no puede quedar vacía.");

  // «Cancelar» drops the draft and gives the focus back to «Editar la transcripción».
  fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByText("Carbón y hierro")).toBeInTheDocument();
  await waitFor(() => expect(edit()).toHaveFocus());
});

it("says so when the server cannot correct transcriptions yet, or cannot be reached", async () => {
  stubApi({ ...ROUTES, [`PUT ${PAGE}/transcription`]: () => jsonResponse({ detail: "Method Not Allowed" }, 405) });
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Editar la transcripción" }));
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(`No se pudo guardar: ${TRANSCRIPTION_EDIT_UNSUPPORTED}`);

  stubApi({ ...ROUTES, [`PUT ${PAGE}/transcription`]: new TypeError("network") });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("No se pudo guardar: No se pudo conectar con el servidor."));
});

it("offers no hand edit outside the overlay, where «Cerrar» is a text button", async () => {
  stubApi(ROUTES);
  renderPanel("panel");
  expect(await screen.findByText("Carbón y hierro")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Editar la transcripción" })).toBeNull();
  expect(screen.getByRole("button", { name: "Cerrar" })).toHaveTextContent("Cerrar");
});

it("offers no hand edit for a page not transcribed yet", async () => {
  stubApi({ [`${PAGE}/meta`]: meta(null), [MD]: jsonResponse({ detail: "No existe." }, 404) });
  renderPanel();
  expect(await screen.findByText("Esta página todavía no está transcrita.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Editar la transcripción" })).toBeNull();
});
