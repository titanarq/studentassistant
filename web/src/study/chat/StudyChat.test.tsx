import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, sseEvent, sseResponse, streamResponse, stubApi } from "../../test/mockApi";
import StudyChat, { BUSY_SENTENCE, MAX_QUESTION_CHARS, STALE_SECTION, THINKING } from "./StudyChat";

const BASE = "/api/subjects/historia/topics/revolucion-industrial";
const TUTOR = `${BASE}/tutor`;
const PAGE = "/subjects/historia/topics/revolucion-industrial";
const SECTIONS = new Map([
  ["contexto", "1. Contexto"],
  ["causas", "2. Causas"],
]);

function turn(overrides: Record<string, unknown> = {}) {
  return {
    time: "2026-09-26T19:04:00Z",
    style: "written",
    question: "¿Qué causas tuvo?",
    reply: "La **población** creció [§causas] y había carbón.[^p2]",
    refs: [{ label: "p2", kind: "notes_page", text: "Apuntes, página 2", source_id: null, path: null }],
    sections: [{ anchor: "causas", title: "2. Causas" }],
    warning: null,
    ...overrides,
  };
}

function answer(overrides: Record<string, unknown> = {}) {
  const { time: _time, ...rest } = turn(overrides);
  return { ...rest, model: "claude-opus" };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderChat(routes: Record<string, Response | (() => Response) | Error>, hasNotes: boolean | null = true) {
  const fetchMock = stubApi({ [TUTOR]: jsonResponse({ turns: [] }), ...routes });
  const onOpenSection = vi.fn();
  const onOpenSource = vi.fn();
  render(
    <StudyChat
      subjectId="historia"
      topicId="revolucion-industrial"
      sections={SECTIONS}
      hasNotes={hasNotes}
      onOpenSection={onOpenSection}
      onOpenSource={onOpenSource}
    />,
  );
  return { fetchMock, onOpenSection, onOpenSource };
}

function log() {
  return screen.getByRole("log", { name: "Preguntas y respuestas" });
}

async function askQuestion(text: string) {
  const input = screen.getByRole("textbox", { name: "Tu pregunta" });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: text } });
  fireEvent.submit(input.closest("form") as HTMLFormElement);
}

it("shows the topic's earlier written questions, not the voice tutor's, as a polite log", async () => {
  renderChat({
    [TUTOR]: jsonResponse({
      turns: [
        { time: "2026-09-25T10:00:00Z", question: "¿Pregunta hablada?", reply: "Respuesta hablada.", refs: [], warning: null },
        turn(),
      ],
    }),
  });

  expect(await within(log()).findByText("¿Qué causas tuvo?")).toBeInTheDocument();
  expect(screen.queryByText("¿Pregunta hablada?")).toBeNull();
  expect(log()).toHaveAttribute("aria-live", "polite");
  // Light Markdown, no raw HTML.
  expect(within(log()).getByText("población").tagName).toBe("STRONG");
});

it("has the read-only line linking to Construir, and a bounded input", async () => {
  renderChat({});

  const note = screen.getByText(/Solo respondo preguntas: no cambio los apuntes\. Para cambiarlos, ve a/);
  expect(note).toHaveTextContent("Solo respondo preguntas: no cambio los apuntes. Para cambiarlos, ve a Construir.");
  expect(within(note).getByRole("link", { name: "Construir" })).toHaveAttribute("href", `${PAGE}/workspace`);
  const input = screen.getByRole("textbox", { name: "Tu pregunta" });
  expect(input).toHaveAttribute("placeholder", "Pregunta sobre el documento…");
  expect(input).toHaveAttribute("maxLength", String(MAX_QUESTION_CHARS));
  expect(await screen.findByText(/Pregunta lo que no entiendas/)).toBeInTheDocument();
});

it("renders both chip kinds inline, with accessible names, and hands them to the page", async () => {
  const { onOpenSection, onOpenSource } = renderChat({ [TUTOR]: jsonResponse({ turns: [turn()] }) });

  const section = await within(log()).findByRole("button", { name: "Ir a la sección 2. Causas" });
  expect(section).toHaveTextContent("§ 2. Causas");
  const source = within(log()).getByRole("button", { name: "Ver la fuente p2" });
  expect(source).toHaveTextContent("p2");
  // Inline: in the paragraph that cites them.
  expect(section.closest("p")).toHaveTextContent("creció § 2. Causas y había carbón.p2");

  fireEvent.click(section);
  expect(onOpenSection).toHaveBeenCalledWith("causas");
  fireEvent.click(source);
  expect(onOpenSource).toHaveBeenCalledWith("p2", source);
});

it("disables a chip for an anchor the document no longer has", async () => {
  const { onOpenSection } = renderChat({
    [TUTOR]: jsonResponse({
      turns: [turn({ reply: "Estaba en [§consecuencias].", sections: [{ anchor: "consecuencias", title: "3. Consecuencias" }] })],
    }),
  });

  const chip = await within(log()).findByRole("button", { name: "Ir a la sección 3. Consecuencias" });
  expect(chip).toBeDisabled();
  expect(chip).toHaveAttribute("title", STALE_SECTION);
  fireEvent.click(chip);
  expect(onOpenSection).not.toHaveBeenCalled();
});

it("says «Pensando…» until the first delta, streams the reply, then shows the answer with its chips", async () => {
  const stream = streamResponse();
  const { fetchMock } = renderChat({ [`POST ${TUTOR}`]: () => stream.response });

  await askQuestion("¿Qué causas tuvo?");

  expect(await within(log()).findByText(THINKING)).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Tu pregunta" })).toBeDisabled();
  stream.push(sseEvent("reply.delta", { text: "La población ", attempt: 1 }));
  expect(await within(log()).findByText(/La población/)).toBeInTheDocument();
  expect(screen.queryByText(THINKING)).toBeNull();
  stream.push(sseEvent("result", answer()));
  stream.close();

  expect(await within(log()).findByRole("button", { name: "Ir a la sección 2. Causas" })).toBeEnabled();
  expect(screen.getByRole("textbox", { name: "Tu pregunta" })).toHaveValue("");

  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(String(post?.[1]?.body))).toEqual({ question: "¿Qué causas tuvo?", confirm_over_cap: false, style: "written" });
  // Nothing but the tutor: never the notes, the chat of Construir or anything that edits.
  expect(fetchMock.mock.calls.every(([url]) => url === TUTOR)).toBe(true);
});

it("offers «Continuar igualmente» past the cost cap and repeats the same question confirmed", async () => {
  let posts = 0;
  const { fetchMock } = renderChat({
    [`POST ${TUTOR}`]: () =>
      ++posts === 1
        ? sseResponse([["error", { status: 409, detail: "Se ha alcanzado el tope de gasto.", code: "cost_cap_reached" }]])
        : sseResponse([["result", answer()]]),
  });

  await askQuestion("¿Qué causas tuvo?");

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Se ha alcanzado el tope de gasto.");
  fireEvent.click(within(alert).getByRole("button", { name: "Continuar igualmente" }));

  expect(await within(log()).findByRole("button", { name: "Ir a la sección 2. Causas" })).toBeInTheDocument();
  const bodies = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST").map(([, init]) => JSON.parse(String(init?.body)));
  expect(bodies).toEqual([
    { question: "¿Qué causas tuvo?", confirm_over_cap: false, style: "written" },
    { question: "¿Qué causas tuvo?", confirm_over_cap: true, style: "written" },
  ]);
  expect(fetchMock.mock.calls.every(([url]) => url === TUTOR)).toBe(true);
});

it("asks to wait when another question of the topic is running", async () => {
  renderChat({
    [`POST ${TUTOR}`]: jsonResponse({ detail: "El tutor ya está contestando otra pregunta de este tema." }, 409),
  });

  await askQuestion("¿Y la máquina de vapor?");

  expect(await screen.findByRole("alert")).toHaveTextContent(BUSY_SENTENCE);
  // The question is kept to send again.
  expect(screen.getByRole("textbox", { name: "Tu pregunta" })).toHaveValue("¿Y la máquina de vapor?");
});

it("says there are no notes yet, linking to Construir, when the topic has none", async () => {
  renderChat(
    {
      [`POST ${TUTOR}`]: jsonResponse(
        { detail: "Todavía no hay apuntes de este tema: prepáralos antes de preguntar por ellos." },
        409,
      ),
    },
    null,
  );

  await askQuestion("¿Qué causas tuvo?");

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Todavía no hay apuntes: constrúyelos en Construir.");
  expect(within(alert).getByRole("link", { name: "Construir" })).toHaveAttribute("href", `${PAGE}/workspace`);
});

it("shows the backend's Spanish detail of any other refusal", async () => {
  renderChat({
    [`POST ${TUTOR}`]: jsonResponse({ detail: "El tutor no está disponible: el servidor no usa Claude." }, 503),
  });

  await askQuestion("¿Qué causas tuvo?");

  expect(await screen.findByRole("alert")).toHaveTextContent("El tutor no está disponible: el servidor no usa Claude.");
  expect(screen.queryByRole("button", { name: "Continuar igualmente" })).toBeNull();
});

it("says when the earlier questions could not be read", async () => {
  renderChat({ [TUTOR]: new Error("offline") });

  expect(await screen.findByRole("alert")).toHaveTextContent(/No se han podido cargar las preguntas anteriores/);
});
