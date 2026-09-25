import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import WebSearchPanel from "./WebSearchPanel";

afterEach(() => {
  vi.unstubAllGlobals();
});

const BASE = "/api/subjects/historia/topics/revolucion-francesa/web-searches";

function search(overrides: Record<string, unknown> = {}) {
  return {
    search_id: "ws-1",
    query: "toma de la Bastilla",
    requested_by: "voice",
    session_id: "20260925-100000",
    queued_at: "2026-09-25T10:00:00Z",
    status: "done",
    results: [
      {
        url: "https://es.wikipedia.org/wiki/Toma_de_la_Bastilla",
        title: "Toma de la Bastilla",
        summary: "Cuenta el 14 de julio de 1789.",
        relevant: true,
        found_in_search: true,
      },
      {
        url: "https://otra.example/bastilla",
        title: "Otra página",
        summary: "",
        relevant: false,
        found_in_search: false,
      },
    ],
    reason: null,
    message: null,
    kept: [],
    ...overrides,
  };
}

function renderPanel(onKept?: () => void) {
  render(<WebSearchPanel subjectId="historia" topicId="revolucion-francesa" onKept={onKept} pollMs={10} />);
}

it("lists the topic's searches and keeps a result as a source", async () => {
  let kept = false;
  const onKept = vi.fn();
  const fetchMock = stubApi({
    [BASE]: () =>
      jsonResponse({
        searches: [
          search(
            kept
              ? { kept: [{ index: 0, url: "u", source_id: "sources/web/001-toma-de-la-bastilla.md", kept_by: "student" }] }
              : {},
          ),
        ],
      }),
    [`POST ${BASE}/ws-1/results/0/keep`]: () => {
      kept = true;
      return jsonResponse({ source_id: "sources/web/001-toma-de-la-bastilla.md", vault_id: "v", title: "T", url: "u" }, 201);
    },
  });
  renderPanel(onKept);

  const list = await screen.findByRole("list", { name: "Búsquedas del tema" });
  expect(list).toHaveTextContent("«toma de la Bastilla» (pedida en voz) — 2 páginas");
  expect(within(list).getByRole("link", { name: "Toma de la Bastilla" })).toHaveAttribute(
    "href",
    "https://es.wikipedia.org/wiki/Toma_de_la_Bastilla",
  );
  expect(list).toHaveTextContent("recomendada");
  expect(list).toHaveTextContent("sin confirmar en la búsqueda");

  fireEvent.click(within(list).getAllByRole("button", { name: "Guardar como fuente" })[0]);
  expect(await screen.findByText(/Guardada como fuente externa \(sources\/web\/001-toma-de-la-bastilla.md\)/)).toBeInTheDocument();
  expect(onKept).toHaveBeenCalledTimes(1);
  expect(fetchMock).toHaveBeenCalledWith(`${BASE}/ws-1/results/0/keep`, { method: "POST" });
});

it("queues a search and polls until it has results", async () => {
  let lists = 0;
  const fetchMock = stubApi({
    [BASE]: () => {
      lists += 1;
      if (lists === 1) return jsonResponse({ searches: [] });
      if (lists === 2) return jsonResponse({ searches: [search({ status: "queued", results: [] })] });
      return jsonResponse({ searches: [search()] });
    },
    [`POST ${BASE}`]: jsonResponse({ search_id: "ws-1", status: "queued" }, 202),
  });
  renderPanel();

  fireEvent.change(await screen.findByLabelText("Qué buscar"), { target: { value: " toma de la Bastilla " } });
  fireEvent.click(screen.getByRole("button", { name: "Buscar" }));

  expect(await screen.findByText(/— Buscando…/)).toBeInTheDocument();
  expect(await screen.findByText(/— 2 páginas/)).toBeInTheDocument();
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(post?.[1]?.body as string)).toEqual({ query: "toma de la Bastilla" });
});

it("shows the backend's refusals", async () => {
  stubApi({
    [BASE]: jsonResponse({ searches: [search({ status: "failed", results: [], message: "Se ha alcanzado el límite de gasto." })] }),
    [`POST ${BASE}`]: jsonResponse({ detail: "La búsqueda en Internet no está disponible en este servidor." }, 503),
  });
  renderPanel();

  expect(await screen.findByText(/No se pudo buscar: Se ha alcanzado el límite de gasto./)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Escribe qué quieres buscar.");
  fireEvent.change(screen.getByLabelText("Qué buscar"), { target: { value: "algo" } });
  fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
  expect(await screen.findByText(/No se ha podido buscar: La búsqueda en Internet no está disponible/)).toBeInTheDocument();
});

it("shows why a result was not kept", async () => {
  stubApi({
    [BASE]: jsonResponse({ searches: [search()] }),
    [`POST ${BASE}/ws-1/results/1/keep`]: jsonResponse({ detail: "La página es un PDF: descárgalo e impórtalo como PDF." }, 422),
  });
  renderPanel();

  const buttons = await screen.findAllByRole("button", { name: "Guardar como fuente" });
  fireEvent.click(buttons[1]);
  expect(await screen.findByText(/No se ha guardado: La página es un PDF/)).toBeInTheDocument();
});
