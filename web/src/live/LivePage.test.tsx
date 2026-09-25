import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import LivePage from "./LivePage";
import { capture, fakeSources, snapshot } from "./testLive";

afterEach(() => {
  vi.unstubAllGlobals();
});

function topics() {
  return stubApi({
    "/api/subjects/fisica/topics": jsonResponse({
      subject_id: "fisica",
      topics: [{ topic_id: "cinematica", subject_id: "fisica", name: "Cinemática" }],
    }),
  });
}

it("says it is connecting, then that no session is running", () => {
  const sources = fakeSources();
  render(<LivePage source={sources.factory} />);
  const last = sources.last;

  expect(screen.getByRole("heading", { name: "Sesión en directo" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Conectando con el servidor");
  expect(last!.url).toBe("/api/live");
  last!.emit("snapshot", snapshot({ session: null }));
  expect(screen.getByRole("status")).toHaveTextContent("No hay ninguna sesión en marcha");
  expect(screen.queryByRole("region", { name: "Transcripción" })).not.toBeInTheDocument();
});

it("shows the transcript, the outline and the captured pages as they arrive", async () => {
  topics();
  const sources = fakeSources();
  render(<LivePage source={sources.factory} />);
  const source = sources.last!;

  source.emit(
    "snapshot",
    snapshot({
      segments: [{ segment_id: "a", t_start: 5_000, t_end: 6_000, text: "Hoy vemos el MRU" }],
      captures: [capture()],
      outline: [{ section_id: "mru", title: "Movimiento rectilíneo", parent_id: null, segment_count: 1 }],
      open_pending: 1,
    }),
  );

  expect(await screen.findByRole("link", { name: "Cinemática" })).toHaveAttribute(
    "href",
    "/subjects/fisica/topics/cinematica",
  );
  const transcript = screen.getByRole("region", { name: "Transcripción" });
  const log = within(transcript).getByRole("log");
  expect(log).toHaveTextContent("00:05 Hoy vemos el MRU");

  source.emit("partial", { segment_id: "b", t_start: 7_000, t_end: 7_500, text: "la velo" });
  expect(transcript).toHaveTextContent("00:07 la velo");
  source.emit("segment", { segment_id: "b", t_start: 7_000, t_end: 8_000, text: "la velocidad es constante" });
  expect(within(log).getAllByRole("listitem")).toHaveLength(2);
  expect(transcript).not.toHaveTextContent("la velo ");
  expect(log).toHaveTextContent("la velocidad es constante");

  const outline = screen.getByRole("region", { name: "Esquema" });
  expect(outline).toHaveTextContent("1 duda por revisar");
  expect(outline).toHaveTextContent("Movimiento rectilíneo · 1 fragmento");
  source.emit("outline", {
    outline: [
      { section_id: "mru", title: "Movimiento rectilíneo", parent_id: null, segment_count: 1 },
      { section_id: "graficas", title: "Gráficas x-t", parent_id: "mru", segment_count: 2 },
    ],
    open_pending: 2,
  });
  expect(outline).toHaveTextContent("2 dudas por revisar");
  const nested = within(outline).getByText("Gráficas x-t", { exact: false });
  expect(nested.closest("ul")?.closest("li")).toHaveTextContent(/^Movimiento rectilíneo/);

  const pages = screen.getByRole("region", { name: "Páginas capturadas" });
  const card = within(pages).getByRole("article", { name: "Apuntes 1" });
  expect(card).toHaveTextContent("01:05");
  expect(card).toHaveTextContent("Transcribiendo…");
  expect(within(card).getByRole("img", { name: "Imagen de Apuntes 1" })).toHaveAttribute(
    "src",
    "/api/sources/subjects/fisica/topics/cinematica/sources/notes/page-001.page.jpg",
  );
  source.emit("capture", capture({ status: "transcribed", text: "MRU: v = cte", page_number: 1 }));
  const transcribed = within(pages).getByRole("article", { name: "Apuntes, página 1" });
  expect(transcribed).toHaveTextContent("Transcrita");
  expect(within(transcribed).getByText("MRU: v = cte")).toBeInTheDocument();
  source.emit("capture", capture({ capture_id: "cap-2", source_context: "book", status: "failed", message: "sin respuesta" }));
  expect(within(pages).getByRole("article", { name: "Libro 2" })).toHaveTextContent(
    "No se pudo transcribir: sin respuesta",
  );
});

it("keeps the ended session on screen and reports a lost connection", async () => {
  topics();
  const sources = fakeSources();
  render(<LivePage source={sources.factory} />);
  const source = sources.last!;
  source.emit("snapshot", snapshot({ segments: [{ segment_id: "a", t_start: 0, t_end: 1, text: "hola" }] }));
  expect(await screen.findByText("Cinemática")).toBeInTheDocument();
  expect(screen.getByText(/en marcha/)).toBeInTheDocument();

  source.drop();
  expect(screen.getByRole("status")).toHaveTextContent("Se ha perdido la conexión con el servidor");
  source.open();
  expect(screen.getByRole("status")).not.toHaveTextContent("Se ha perdido la conexión");

  source.emit("ended", { session_id: "s-1" });
  expect(screen.getByRole("status")).toHaveTextContent("La sesión ha terminado.");
  source.emit("snapshot", snapshot({ session: null }));
  expect(screen.getByRole("status")).toHaveTextContent("La sesión ha terminado.");
  expect(screen.getByRole("log")).toHaveTextContent("hola");
  expect(screen.queryByText(/en marcha/)).not.toBeInTheDocument();
});

it("closes the stream when the page goes away", () => {
  const sources = fakeSources();
  const { unmount } = render(<LivePage source={sources.factory} />);
  unmount();
  expect(sources.last!.closed).toBe(true);
});
