import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PAUSED_BANNER } from "../desk/DeskCost";
import { jsonResponse, stubApi } from "../test/mockApi";
import { UNPRICED_WARNING } from "../topic/TopicCostBlock";
import WorkspaceCost from "./WorkspaceCost";

const BASE = "/api/subjects/historia/topics/revolucion-industrial";
const SESSION_COST = "/api/cost?subject=historia&topic=revolucion-industrial&session=s-20260926-1000";

afterEach(() => {
  vi.unstubAllGlobals();
});

function status(overrides: Record<string, unknown> = {}) {
  return jsonResponse({
    session_usd: 0,
    day_usd: 0.25,
    max_usd_per_session: 1,
    max_usd_per_day: 2,
    observer_paused: false,
    editor_needs_confirmation: false,
    unpriced_session_calls: 0,
    unpriced_day_calls: 0,
    unpriced_models: [],
    ...overrides,
  });
}

function totals(usd: number, unpriced = 0) {
  return {
    usd,
    tokens: 1000,
    input_tokens: 800,
    output_tokens: 200,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    calls: 3,
    unpriced_calls: unpriced,
  };
}

function topicCost(usd: number, unpriced = 0) {
  return jsonResponse({
    subject_id: "historia",
    topic_id: "revolucion-industrial",
    total: totals(usd, unpriced),
    sessions: [],
    no_session: totals(0),
  });
}

function renderCost(sessionId: string | null, refreshKey: string | number = 0) {
  return render(
    <WorkspaceCost subjectId="historia" topicId="revolucion-industrial" sessionId={sessionId} refreshKey={refreshKey} />,
  );
}

it("shows the open session's spend, formatted as the topic page formats amounts", async () => {
  stubApi({ [SESSION_COST]: status({ session_usd: 0.1234 }) });

  renderCost("s-20260926-1000");

  const line = await screen.findByText("Esta sesión: 0,1234 USD");
  expect(line).not.toHaveAttribute("title");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("shows the topic's total without an open session", async () => {
  stubApi({ [`${BASE}/cost`]: topicCost(1.5), "/api/cost": status() });

  renderCost(null);

  expect(await screen.findByText("Este tema: 1,5000 USD")).toBeInTheDocument();
  expect(screen.queryByText(/Esta sesión/)).not.toBeInTheDocument();
});

it("adds the desk's warning when a cap is reached", async () => {
  stubApi({ [SESSION_COST]: status({ session_usd: 1, observer_paused: true, editor_needs_confirmation: true }) });

  renderCost("s-20260926-1000");

  expect(await screen.findByRole("alert")).toHaveTextContent(PAUSED_BANNER);
  expect(screen.getByText("Esta sesión: 1,0000 USD")).toBeInTheDocument();
});

it("warns on the topic's line too when today's cap is reached", async () => {
  stubApi({ [`${BASE}/cost`]: topicCost(2), "/api/cost": status({ day_usd: 2, observer_paused: true }) });

  renderCost(null);

  expect(await screen.findByRole("alert")).toHaveTextContent(PAUSED_BANNER);
});

it("puts the unpriced warning in the line's title", async () => {
  stubApi({ [`${BASE}/cost`]: topicCost(0.2, 2), "/api/cost": status() });

  renderCost(null);

  expect(await screen.findByText("Este tema: 0,2000 USD")).toHaveAttribute("title", UNPRICED_WARNING);
});

it("puts the unpriced warning in the title of the session's line", async () => {
  stubApi({ [SESSION_COST]: status({ session_usd: 0.3, unpriced_session_calls: 1 }) });

  renderCost("s-20260926-1000");

  expect(await screen.findByText("Esta sesión: 0,3000 USD")).toHaveAttribute("title", UNPRICED_WARNING);
});

it("shows nothing when the read fails, and no error", async () => {
  const fetchMock = stubApi({ [`${BASE}/cost`]: jsonResponse({ detail: "boom" }, 500), "/api/cost": status() });

  const { container } = renderCost(null);

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(container).toBeEmptyDOMElement();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("shows nothing while the first read is on its way", () => {
  stubApi({ [SESSION_COST]: () => new Promise<Response>(() => undefined) as unknown as Response });

  const { container } = renderCost("s-20260926-1000");

  expect(container).toBeEmptyDOMElement();
});

it("reads again when the refresh key changes", async () => {
  let reads = 0;
  stubApi({ [SESSION_COST]: () => status({ session_usd: reads++ === 0 ? 0.1 : 0.2 }) });

  const { rerender } = renderCost("s-20260926-1000", "a");
  expect(await screen.findByText("Esta sesión: 0,1000 USD")).toBeInTheDocument();

  rerender(<WorkspaceCost subjectId="historia" topicId="revolucion-industrial" sessionId="s-20260926-1000" refreshKey="b" />);

  expect(await screen.findByText("Esta sesión: 0,2000 USD")).toBeInTheDocument();
  expect(reads).toBe(2);
});
