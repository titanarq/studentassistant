import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import TopicCostBlock, { UNPRICED_WARNING } from "./TopicCostBlock";

afterEach(() => {
  vi.unstubAllGlobals();
});

const COST_PATH = "/api/subjects/historia/topics/revolucion-francesa/cost";
// 2026-09-24 12:00 UTC: the same calendar day in every time zone the tests may run in.
const SEP_24 = Date.UTC(2026, 8, 24, 12);

function totals(usd: number, tokens: number, calls: number, unpriced = 0) {
  return {
    usd,
    tokens,
    input_tokens: tokens,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    calls,
    unpriced_calls: unpriced,
  };
}

function cost({ unpriced = 0, noSession = 1 }: { unpriced?: number; noSession?: number } = {}) {
  return jsonResponse({
    subject_id: "historia",
    topic_id: "revolucion-francesa",
    total: totals(1.5, 12345, 3 + noSession, unpriced),
    sessions: [
      { ...totals(0.25, 580, 2), session_id: "20260924-120000", started_at_ms: SEP_24 },
      { ...totals(0.75, 100, 1, unpriced), session_id: "20260920-080000", started_at_ms: null },
    ],
    no_session: totals(0.5, 200, noSession),
  });
}

function renderBlock() {
  render(<TopicCostBlock subjectId="historia" topicId="revolucion-francesa" />);
}

it("shows the topic total and one row per session plus the calls outside them, in USD", async () => {
  stubApi({ [COST_PATH]: cost() });

  renderBlock();

  const block = await screen.findByRole("region", { name: "Coste" });
  expect(await within(block).findByText(/Total del tema/)).toHaveTextContent(
    "Total del tema: 1,5000 USD · 12.345 tokens · 4 llamadas",
  );
  const rows = within(within(block).getByRole("list", { name: "Coste por sesión" })).getAllByRole("listitem");
  expect(rows.map((row) => row.textContent)).toEqual([
    "Sesión del 24 de septiembre de 2026: 0,2500 USD · 580 tokens · 2 llamadas",
    "Sesión 20260920-080000: 0,7500 USD · 100 tokens · 1 llamada",
    "Fuera de las sesiones (editor, material): 0,5000 USD · 200 tokens · 1 llamada",
  ]);
  expect(within(block).queryByText(new RegExp(UNPRICED_WARNING))).not.toBeInTheDocument();
});

it("warns that the total falls short when some calls have no known price", async () => {
  stubApi({ [COST_PATH]: cost({ unpriced: 1, noSession: 0 }) });

  renderBlock();

  expect(await screen.findByRole("note")).toHaveTextContent(`${UNPRICED_WARNING}.`);
  expect(screen.queryByText(/Fuera de las sesiones/)).not.toBeInTheDocument();
});

it("says there is no spend yet for a topic without sessions or calls", async () => {
  stubApi({
    [COST_PATH]: jsonResponse({
      subject_id: "historia",
      topic_id: "revolucion-francesa",
      total: totals(0, 0, 0),
      sessions: [],
      no_session: totals(0, 0, 0),
    }),
  });

  renderBlock();

  expect(await screen.findByText("Todavía no hay gasto en este tema.")).toBeInTheDocument();
});

it("reports a failed read in Spanish without an alert", async () => {
  stubApi({ [COST_PATH]: new Error("offline") });

  renderBlock();

  expect(await screen.findByText("No se pudo cargar el coste: No se pudo conectar con el servidor.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
