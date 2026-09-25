import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import DeskCost, { PAUSED_BANNER } from "./DeskCost";

afterEach(() => {
  vi.unstubAllGlobals();
});

function status(overrides: Record<string, unknown> = {}) {
  return jsonResponse({
    session_usd: 0,
    day_usd: 0.25,
    max_usd_per_session: null,
    max_usd_per_day: 2,
    observer_paused: false,
    editor_needs_confirmation: false,
    unpriced_session_calls: 0,
    unpriced_day_calls: 0,
    unpriced_models: [],
    ...overrides,
  });
}

it("shows today's spend against the daily cap and no banner under the caps", async () => {
  stubApi({ "/api/cost": status() });

  render(<DeskCost />);

  const block = await screen.findByRole("region", { name: "Gasto de hoy" });
  expect(await within(block).findByText(/Hoy \(UTC\)/)).toHaveTextContent("Hoy (UTC): 0,2500 USD de 2,0000 USD");
  expect(within(block).queryByText(/Sesión abierta/)).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("note")).not.toBeInTheDocument();
});

it("asks for the open session's spend and shows it against the session cap", async () => {
  const fetchMock = stubApi({
    "/api/cost?subject=historia&topic=revolucion-francesa&session=20260924-120000": status({
      session_usd: 0.5,
      max_usd_per_session: 1,
      max_usd_per_day: null,
    }),
  });

  render(
    <DeskCost session={{ subjectId: "historia", topicId: "revolucion-francesa", sessionId: "20260924-120000" }} />,
  );

  expect(await screen.findByText(/Sesión abierta/)).toHaveTextContent("Sesión abierta: 0,5000 USD de 1,0000 USD");
  expect(screen.getByText(/Hoy \(UTC\)/)).toHaveTextContent("Hoy (UTC): 0,2500 USD (sin límite)");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it.each([
  ["the observer is paused", { observer_paused: true }],
  ["the editor needs confirmation", { editor_needs_confirmation: true }],
])("shows the cap banner when %s", async (_, flags) => {
  stubApi({ "/api/cost": status({ day_usd: 2, ...flags }) });

  render(<DeskCost />);

  expect(await screen.findByRole("alert")).toHaveTextContent(PAUSED_BANNER);
});

it("notes that the spend is underestimated when some calls have no known price", async () => {
  stubApi({ "/api/cost": status({ unpriced_day_calls: 2, unpriced_models: ["claude-mystery-1"] }) });

  render(<DeskCost />);

  expect(await screen.findByRole("note")).toHaveTextContent("Hay llamadas sin precio conocido");
});

it("reports a failed read in Spanish without an alert", async () => {
  stubApi({ "/api/cost": jsonResponse({ detail: "x" }, 503) });

  render(<DeskCost />);

  expect(await screen.findByText("No se pudo cargar el gasto: El servidor respondió con un error (503).")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
