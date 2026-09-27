import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, sseEvent, sseResponse, streamResponse, stubApi } from "../../test/mockApi";
import type { VoiceQuestionCallbacks, VoiceQuestionStarter } from "../../tutor/voiceQuestion";
import StudyChat, { BUSY_SENTENCE, FOLLOW_BUTTON, MAX_QUESTION_CHARS, STALE_SECTION, THINKING } from "./StudyChat";

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

function renderChat(
  routes: Record<string, Response | (() => Response) | Error>,
  hasNotes: boolean | null = true,
  suggestion: { text: string; id: number } | null = null,
  voice: { listen?: VoiceQuestionStarter; voiceSupported?: boolean } = {},
) {
  const fetchMock = stubApi({ [TUTOR]: jsonResponse({ turns: [] }), ...routes });
  const onOpenSection = vi.fn();
  const onOpenSource = vi.fn();
  const onGenerated = vi.fn();
  const onOpenOption = vi.fn();
  const chat = (value: { text: string; id: number } | null) => (
    <StudyChat
      subjectId="historia"
      topicId="revolucion-industrial"
      sections={SECTIONS}
      hasNotes={hasNotes}
      onOpenSection={onOpenSection}
      onOpenSource={onOpenSource}
      onGenerated={onGenerated}
      onOpenOption={onOpenOption}
      suggestion={value}
      listen={voice.listen}
      voiceSupported={voice.voiceSupported}
    />
  );
  const { rerender, unmount } = render(chat(suggestion));
  return {
    unmount,
    fetchMock,
    onOpenSection,
    onOpenSource,
    onGenerated,
    onOpenOption,
    suggest: (value: { text: string; id: number }) => rerender(chat(value)),
  };
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

it("shows the topic's earlier written questions, not the voice tutor's, as a log that is not announced", async () => {
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
  // The log itself is not a live region, and the history is not announced (#412).
  expect(log()).toHaveAttribute("aria-live", "off");
  expect(screen.getByTestId("study-chat-latest")).toHaveTextContent(/^$/);
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

// ---- Generation requests («hazme un quiz», #366, #367) ----

const STUDY = {
  subject: "historia",
  topic: "revolucion-industrial",
  study_version: { version: 4, tag: "historia/revolucion-industrial/apuntes-v4" },
  study_current: true,
  options: [{ key: "quiz", kind: "quiz", state: "listo", stale_reason: null }],
};

function started(option = "quiz", text = "Preparando un quiz de 10 preguntas con tus apuntes v4…") {
  return { kind: option === "tarjetas" ? "flashcards" : option, option, text };
}

function generated(overrides: Record<string, unknown> = {}) {
  return {
    kind: "generation",
    option: "quiz",
    material_kind: "quiz",
    reply: "Listo: 10 preguntas. Ábrelo en «Quiz».",
    items: 10,
    warnings: [],
    study: STUDY,
    ...overrides,
  };
}

it("shows a generation's progress line, then its reply, warnings and «Abrir» into the option", async () => {
  const stream = streamResponse();
  const { onGenerated, onOpenOption } = renderChat({ [`POST ${TUTOR}`]: () => stream.response });

  await askQuestion("hazme un quiz");
  stream.push(sseEvent("generation.started", started()));

  const progress = await within(log()).findByText("Preparando un quiz de 10 preguntas con tus apuntes v4…");
  expect(progress.closest("li")).toHaveAttribute("aria-busy", "true");
  expect(progress.querySelector(".study-chat-spinner")).not.toBeNull();
  expect(screen.queryByText(THINKING)).toBeNull();
  expect(screen.getByRole("textbox", { name: "Tu pregunta" })).toBeDisabled();

  stream.push(sseEvent("result", generated({ warnings: ["Dos preguntas citan poco los apuntes."] })));
  stream.close();

  expect(await within(log()).findByText("Listo: 10 preguntas. Ábrelo en «Quiz».")).toBeInTheDocument();
  expect(within(log()).getByText("Dos preguntas citan poco los apuntes.")).toHaveClass("chat-warning");
  expect(screen.queryByText(/Preparando un quiz/)).toBeNull();
  expect(screen.getByRole("textbox", { name: "Tu pregunta" })).toBeEnabled();
  expect(onGenerated).toHaveBeenCalledTimes(1);
  expect(onGenerated.mock.calls[0][0]).toMatchObject({
    option: "quiz",
    materialKind: "quiz",
    items: 10,
    study: { options: [{ key: "quiz", state: "ready" }] },
  });

  fireEvent.click(within(log()).getByRole("button", { name: "Abrir «Quiz»" }));
  expect(onOpenOption).toHaveBeenCalledWith("quiz");
});

it("names the option in its title: «Abrir «Tarjetas de memoria»»", async () => {
  renderChat({
    [`POST ${TUTOR}`]: () =>
      sseResponse([
        ["generation.started", started("tarjetas", "Preparando tarjetas…")],
        ["result", generated({ option: "tarjetas", material_kind: "flashcards", reply: "Listas: 20 tarjetas." })],
      ]),
  });

  await askQuestion("hazme tarjetas de memoria");

  expect(await within(log()).findByRole("button", { name: "Abrir «Tarjetas de memoria»" })).toBeInTheDocument();
});

it("gives the slides' reply with «Abrir «Diapositivas»»", async () => {
  const { onGenerated, onOpenOption } = renderChat({
    [`POST ${TUTOR}`]: () =>
      sseResponse([
        ["generation.started", started("diapositivas", "Preparando las diapositivas…")],
        [
          "result",
          generated({
            option: "diapositivas",
            material_kind: "diapositivas",
            reply: "Listas: 8 diapositivas. Ábrelas en «Diapositivas».",
          }),
        ],
      ]),
  });

  await askQuestion("hazme diapositivas");

  expect(await within(log()).findByText("Listas: 8 diapositivas. Ábrelas en «Diapositivas».")).toBeInTheDocument();
  expect(onGenerated).toHaveBeenCalledTimes(1);
  fireEvent.click(within(log()).getByRole("button", { name: "Abrir «Diapositivas»" }));
  expect(onOpenOption).toHaveBeenCalledWith("diapositivas");
});

it("past the cost cap, «Continuar igualmente» repeats the generation request confirmed", async () => {
  let posts = 0;
  const { fetchMock } = renderChat({
    [`POST ${TUTOR}`]: () =>
      ++posts === 1
        ? sseResponse([
            ["generation.started", started()],
            ["error", { status: 409, detail: "Se ha alcanzado el tope de gasto de hoy.", code: "cost_cap_reached" }],
          ])
        : sseResponse([
            ["generation.started", started()],
            ["result", generated()],
          ]),
  });

  await askQuestion("hazme un quiz");

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Se ha alcanzado el tope de gasto de hoy.");
  // A failed generation leaves no turn in the log.
  expect(within(log()).queryByText(/Preparando/)).toBeNull();
  fireEvent.click(within(alert).getByRole("button", { name: "Continuar igualmente" }));

  expect(await within(log()).findByRole("button", { name: "Abrir «Quiz»" })).toBeInTheDocument();
  const bodies = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST").map(([, init]) => JSON.parse(String(init?.body)));
  expect(bodies).toEqual([
    { question: "hazme un quiz", confirm_over_cap: false, style: "written" },
    { question: "hazme un quiz", confirm_over_cap: true, style: "written" },
  ]);
});

it("shows a failed generation's Spanish detail and keeps the request to send again", async () => {
  const { onGenerated } = renderChat({
    [`POST ${TUTOR}`]: () =>
      sseResponse([
        ["generation.started", started()],
        ["error", { status: 502, detail: "No se pudo generar el quiz: inténtalo de nuevo." }],
      ]),
  });

  await askQuestion("hazme un quiz");

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo generar el quiz: inténtalo de nuevo.");
  expect(screen.queryByRole("button", { name: "Continuar igualmente" })).toBeNull();
  expect(screen.getByRole("textbox", { name: "Tu pregunta" })).toHaveValue("hazme un quiz");
  expect(onGenerated).not.toHaveBeenCalled();
});

it("says to wait when the same material is already being generated", async () => {
  renderChat({
    [`POST ${TUTOR}`]: () => sseResponse([["error", { status: 409, detail: "Ya se está generando el quiz de este tema." }]]),
  });

  await askQuestion("hazme un quiz");

  expect(await screen.findByRole("alert")).toHaveTextContent(BUSY_SENTENCE);
});

it("shows a generation turn of the history finished, with «Abrir», and older answers unchanged", async () => {
  const { onOpenOption } = renderChat({
    [TUTOR]: jsonResponse({
      turns: [
        turn(),
        {
          time: "2026-09-26T19:10:00Z",
          style: "written",
          kind: "generation",
          question: "hazme ejercicios",
          reply: "Listo: 5 ejercicios. Ábrelo en «Ejercicios».",
          option: "ejercicios",
          items: 5,
          refs: [],
          sections: [],
          warning: null,
        },
      ],
    }),
  });

  expect(await within(log()).findByText("Listo: 5 ejercicios. Ábrelo en «Ejercicios».")).toBeInTheDocument();
  expect(within(log()).getByText("hazme ejercicios")).toBeInTheDocument();
  expect(within(log()).queryByText(/Preparando/)).toBeNull();
  // The older answer keeps its chips.
  expect(within(log()).getByRole("button", { name: "Ir a la sección 2. Causas" })).toBeInTheDocument();
  expect(within(log()).getAllByRole("button", { name: /^Abrir/ })).toHaveLength(1);

  fireEvent.click(within(log()).getByRole("button", { name: "Abrir «Ejercicios»" }));
  expect(onOpenOption).toHaveBeenCalledWith("ejercicios");
});

it("reads a result with kind «answer» as an ordinary answer, with no «Abrir»", async () => {
  const { onGenerated } = renderChat({ [`POST ${TUTOR}`]: () => sseResponse([["result", { ...answer(), kind: "answer" }]]) });

  await askQuestion("¿Qué causas tuvo?");

  expect(await within(log()).findByRole("button", { name: "Ir a la sección 2. Causas" })).toBeInTheDocument();
  expect(within(log()).queryByRole("button", { name: /^Abrir/ })).toBeNull();
  expect(onGenerated).not.toHaveBeenCalled();
});

it("puts a suggested phrase in the input without sending it", async () => {
  const { fetchMock, suggest } = renderChat({});
  const input = screen.getByRole("textbox", { name: "Tu pregunta" });
  await waitFor(() => expect(input).toBeEnabled());

  suggest({ text: "hazme un quiz", id: 1 });

  await waitFor(() => expect(input).toHaveValue("hazme un quiz"));
  expect(input).toHaveFocus();
  fireEvent.change(input, { target: { value: "" } });
  // The same phrase again (a new id) puts it back.
  suggest({ text: "hazme un quiz", id: 2 });
  await waitFor(() => expect(input).toHaveValue("hazme un quiz"));
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

// ---- #412: the log follows the newest turn, only the latest turn is announced ----

/** Gives the log a layout (jsdom has none): its height, its content's height and a spied scrollTop. */
function layOut(element: HTMLElement, { height = 200, content = 1000 } = {}) {
  let top = 0;
  const sets = vi.fn((value: number) => {
    top = value;
  });
  Object.defineProperty(element, "clientHeight", { configurable: true, get: () => height });
  Object.defineProperty(element, "scrollHeight", { configurable: true, get: () => content });
  Object.defineProperty(element, "scrollTop", { configurable: true, get: () => top, set: sets });
  return {
    sets,
    grow: (by: number) => {
      content += by;
    },
    scrollTo: (value: number) => {
      top = value;
      fireEvent.scroll(element);
    },
  };
}

it("follows the streamed answer to the end of the log, and announces only the finished latest turn", async () => {
  const stream = streamResponse();
  renderChat({ [TUTOR]: jsonResponse({ turns: [turn()] }), [`POST ${TUTOR}`]: () => stream.response });
  await within(log()).findByText("¿Qué causas tuvo?");
  const latest = screen.getByTestId("study-chat-latest");
  expect(latest).toHaveAttribute("aria-live", "polite");
  expect(latest).toBeEmptyDOMElement();
  const area = layOut(log());

  await askQuestion("¿Y las consecuencias?");
  await waitFor(() => expect(area.sets).toHaveBeenLastCalledWith(1000));
  await waitFor(() => expect(latest).toHaveTextContent(`Asistente: ${THINKING}`));

  area.sets.mockClear();
  area.grow(250);
  stream.push(sseEvent("reply.delta", { text: "Crecieron las ciudades", attempt: 1 }));
  await within(log()).findByText(/Crecieron las ciudades/);
  await waitFor(() => expect(area.sets).toHaveBeenLastCalledWith(1250));
  // A fragment is not read out.
  expect(latest).toHaveTextContent(`Asistente: ${THINKING}`);

  stream.push(sseEvent("result", answer({ question: "¿Y las consecuencias?", reply: "Crecieron las ciudades [§causas].[^p2]" })));
  stream.close();
  await waitFor(() => expect(latest).toHaveTextContent(/^Asistente: Crecieron las ciudades\.$/));
  expect(latest).not.toHaveTextContent("población");
});

it("does not scroll when the student scrolled up, and offers «Nuevos mensajes ↓» instead", async () => {
  const stream = streamResponse();
  renderChat({ [TUTOR]: jsonResponse({ turns: [turn()] }), [`POST ${TUTOR}`]: () => stream.response });
  await within(log()).findByText("¿Qué causas tuvo?");
  const area = layOut(log());
  await askQuestion("¿Y las consecuencias?");
  await within(log()).findByText(THINKING);
  // The student reads the earlier answer.
  area.scrollTo(0);
  area.sets.mockClear();

  area.grow(300);
  stream.push(sseEvent("reply.delta", { text: "Crecieron las ciudades", attempt: 1 }));
  await within(log()).findByText(/Crecieron las ciudades/);
  expect(area.sets).not.toHaveBeenCalled();

  fireEvent.click(await screen.findByRole("button", { name: FOLLOW_BUTTON }));
  expect(area.sets).toHaveBeenLastCalledWith(1300);
  expect(screen.queryByRole("button", { name: FOLLOW_BUTTON })).toBeNull();
  stream.close();
});

function fakeListen() {
  const calls: VoiceQuestionCallbacks[] = [];
  const stop = vi.fn();
  const listen: VoiceQuestionStarter = (callbacks) => {
    calls.push(callbacks);
    return { stop };
  };
  return { listen, calls, stop };
}

const SPEAK = "Dictar el mensaje por voz";
const LISTENING = "Escuchando… (pulsa para parar)";

it("asks a spoken question once, showing the interim text in the input", async () => {
  const stream = streamResponse();
  const { listen, calls } = fakeListen();
  const { fetchMock } = renderChat({ [`POST ${TUTOR}`]: () => stream.response }, true, null, { listen, voiceSupported: true });
  const speak = screen.getByRole("button", { name: SPEAK });
  await waitFor(() => expect(speak).toBeEnabled());
  fireEvent.click(speak);
  expect(screen.getByRole("button", { name: LISTENING })).toHaveAttribute("aria-pressed", "true");
  act(() => calls[0].onInterim?.("¿Qué causas"));
  expect(screen.getByRole("textbox", { name: "Tu pregunta" })).toHaveValue("¿Qué causas");
  act(() => {
    calls[0].onFinal("¿Qué causas tuvo?");
    calls[0].onEnd?.();
  });
  expect(await within(log()).findByText(THINKING)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: SPEAK })).toBeDisabled();
  stream.push(sseEvent("result", answer()));
  stream.close();
  expect(await within(log()).findByRole("button", { name: "Ir a la sección 2. Causas" })).toBeEnabled();
  const bodies = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST").map(([, init]) => JSON.parse(String(init?.body)));
  expect(bodies).toEqual([{ question: "¿Qué causas tuvo?", confirm_over_cap: false, style: "written" }]);
  expect(screen.getByRole("textbox", { name: "Tu pregunta" })).toHaveValue("");
});

it("trims a spoken question to the cap", async () => {
  const { listen, calls } = fakeListen();
  const { fetchMock } = renderChat({ [`POST ${TUTOR}`]: sseResponse([["result", answer()]]) }, true, null, { listen, voiceSupported: true });
  const speak = screen.getByRole("button", { name: SPEAK });
  await waitFor(() => expect(speak).toBeEnabled());
  fireEvent.click(speak);
  act(() => {
    calls[0].onFinal("a".repeat(MAX_QUESTION_CHARS + 50));
    calls[0].onEnd?.();
  });
  await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(String(post?.[1]?.body)).question).toHaveLength(MAX_QUESTION_CHARS);
});

it("stops listening when pressed again, and shows a problem's line", async () => {
  const { listen, calls, stop } = fakeListen();
  renderChat({}, true, null, { listen, voiceSupported: true });
  const speak = screen.getByRole("button", { name: SPEAK });
  await waitFor(() => expect(speak).toBeEnabled());
  fireEvent.click(speak);
  fireEvent.click(screen.getByRole("button", { name: LISTENING }));
  expect(stop).toHaveBeenCalledTimes(1);
  act(() => {
    calls[0].onProblem("no-speech");
    calls[0].onEnd?.();
  });
  expect(screen.getByRole("alert")).toHaveTextContent("No te he oído. Pulsa «Hablar» y habla.");
});

it("offers a disabled «Hablar» with a hint in a browser without speech recognition", async () => {
  renderChat({}, true, null, { voiceSupported: false });
  const speak = screen.getByRole("button", { name: SPEAK });
  expect(speak).toBeDisabled();
  expect(speak).toHaveAccessibleDescription("Este navegador no reconoce la voz: escribe tu mensaje.");
  const input = screen.getByRole("textbox", { name: "Tu pregunta" });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: "¿Qué causas tuvo?" } });
  expect(screen.getByRole("button", { name: "Preguntar" })).toBeEnabled();
});

it("stops a running recognition on unmount", async () => {
  const { listen, stop } = fakeListen();
  const { unmount } = renderChat({}, true, null, { listen, voiceSupported: true });
  const speak = screen.getByRole("button", { name: SPEAK });
  await waitFor(() => expect(speak).toBeEnabled());
  fireEvent.click(speak);
  unmount();
  expect(stop).toHaveBeenCalledTimes(1);
});
