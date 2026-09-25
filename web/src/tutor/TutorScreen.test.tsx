import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, sseEvent, sseResponse, streamResponse, stubApi } from "../test/mockApi";
import type { SpeechOutput } from "./speech";
import TutorScreen from "./TutorScreen";
import type { VoiceQuestionCallbacks, VoiceQuestionStarter } from "./voiceQuestion";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const TUTOR = "/api/subjects/historia/topics/revolucion-industrial/tutor";

const REFS = [{ label: "p1", kind: "notes", text: "Apuntes, página 1", source_id: "sources/notes/page-001.jpg" }];

function answer(question: string, reply: string, extra: Record<string, unknown> = {}) {
  return { subject: "historia", topic: "revolucion-industrial", question, reply, refs: REFS, warning: null, ...extra };
}

function fakeSpeech(supported = true) {
  const speak = vi.fn<(text: string, onEnd?: () => void) => void>();
  const cancel = vi.fn();
  const output: SpeechOutput = { supported, speak, cancel };
  return { output, speak, cancel };
}

function fakeListen() {
  const calls: VoiceQuestionCallbacks[] = [];
  const stop = vi.fn();
  const listen: VoiceQuestionStarter = (callbacks) => {
    calls.push(callbacks);
    return { stop };
  };
  return { listen, calls, stop };
}

function renderScreen(props: Partial<Parameters<typeof TutorScreen>[0]> = {}) {
  return render(
    <TutorScreen
      subjectId="historia"
      topicId="revolucion-industrial"
      subjectName="Historia"
      topicName="La Revolución Industrial"
      voiceSupported
      {...props}
    />,
  );
}

function bodyOf(call: unknown[]): Record<string, unknown> {
  return JSON.parse((call[1] as RequestInit).body as string);
}

it("shows the earlier questions and asks a typed one, reading the answer aloud", async () => {
  const stream = streamResponse();
  const fetchMock = stubApi({
    [TUTOR]: jsonResponse({
      subject: "historia",
      topic: "revolucion-industrial",
      turns: [{ time: "2026-09-25T10:00:00Z", ...answer("¿Cuándo empezó?", "En el siglo XVIII.[^p1]") }],
    }),
    [`POST ${TUTOR}`]: () => stream.response,
  });
  const speech = fakeSpeech();
  renderScreen({ speech: speech.output, listen: fakeListen().listen });

  expect(await screen.findByText("En el siglo XVIII.[p1]", { exact: false })).toBeInTheDocument();
  expect(screen.getByText("[p1] Apuntes, página 1")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Ver los apuntes del tema" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-industrial/notes",
  );

  fireEvent.change(screen.getByLabelText("Escribe tu pregunta"), { target: { value: "¿Qué es una fábrica?" } });
  fireEvent.click(screen.getByRole("button", { name: "Preguntar" }));
  expect(await screen.findByText("El tutor está pensando…")).toBeInTheDocument();
  const [post] = fetchMock.mock.calls.filter((call) => (call[1] as RequestInit | undefined)?.method === "POST");
  expect(bodyOf(post)).toEqual({ question: "¿Qué es una fábrica?", confirm_over_cap: false });

  await act(async () => {
    stream.push(sseEvent("reply.delta", { text: "Un lugar", attempt: 1 }));
  });
  expect(await screen.findByText("Un lugar", { exact: false })).toBeInTheDocument();
  await act(async () => {
    stream.push(sseEvent("result", answer("¿Qué es una fábrica?", "Un lugar de producción.[^p1]")));
    stream.close();
  });
  expect(await screen.findByText("Un lugar de producción.[p1]", { exact: false })).toBeInTheDocument();
  expect(speech.speak).toHaveBeenCalledWith("Un lugar de producción.", expect.any(Function));
  expect(screen.getByLabelText("Escribe tu pregunta")).toHaveValue("");

  // "Parar de leer" while it reads; the end of the reading hides it.
  fireEvent.click(screen.getByRole("button", { name: "Parar de leer" }));
  expect(speech.cancel).toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Parar de leer" })).not.toBeInTheDocument();
});

it("asks a spoken question and sends it as soon as it is recognized", async () => {
  const fetchMock = stubApi({
    [TUTOR]: jsonResponse({ subject: "historia", topic: "revolucion-industrial", turns: [] }),
    [`POST ${TUTOR}`]: () => sseResponse([["result", answer("¿qué es el vapor?", "Una fuerza.[^p1]")]]),
  });
  const speech = fakeSpeech();
  const voice = fakeListen();
  renderScreen({ speech: speech.output, listen: voice.listen });
  await screen.findByText("Todavía no le has preguntado nada sobre este tema.");

  fireEvent.click(screen.getByRole("button", { name: "Preguntar por voz" }));
  const listening = screen.getByRole("button", { name: "Escuchando… (pulsa para terminar)" });
  expect(listening).toHaveAttribute("aria-pressed", "true");
  act(() => voice.calls[0].onInterim?.("qué es el"));
  expect(screen.getByRole("status", { name: "Lo que te oigo" })).toHaveTextContent("qué es el");
  fireEvent.click(listening);
  expect(voice.stop).toHaveBeenCalled();
  act(() => {
    voice.calls[0].onFinal("¿qué es el vapor?");
    voice.calls[0].onEnd?.();
  });

  expect(await screen.findByText("Una fuerza.[p1]", { exact: false })).toBeInTheDocument();
  const [post] = fetchMock.mock.calls.filter((call) => (call[1] as RequestInit | undefined)?.method === "POST");
  expect(bodyOf(post).question).toBe("¿qué es el vapor?");
  expect(speech.speak).toHaveBeenCalledWith("Una fuerza.", expect.any(Function));
  expect(screen.getByRole("button", { name: "Preguntar por voz" })).toBeEnabled();
});

it("explains voice problems and lets the student type instead", async () => {
  stubApi({ [TUTOR]: jsonResponse({ subject: "historia", topic: "revolucion-industrial", turns: [] }) });
  const voice = fakeListen();
  renderScreen({ speech: fakeSpeech().output, listen: voice.listen });
  await screen.findByText("Todavía no le has preguntado nada sobre este tema.");

  fireEvent.click(screen.getByRole("button", { name: "Preguntar por voz" }));
  act(() => {
    voice.calls[0].onProblem("permission-denied");
    voice.calls[0].onEnd?.();
  });
  expect(screen.getByRole("alert")).toHaveTextContent("No hay permiso para usar el micrófono");
  expect(screen.getByLabelText("Escribe tu pregunta")).toBeEnabled();
});

it("works without speech recognition nor synthesis", async () => {
  stubApi({ [TUTOR]: jsonResponse({ subject: "historia", topic: "revolucion-industrial", turns: [] }) });
  renderScreen({ speech: fakeSpeech(false).output, listen: fakeListen().listen, voiceSupported: false });
  await screen.findByText("Todavía no le has preguntado nada sobre este tema.");
  expect(screen.getByRole("button", { name: "Preguntar por voz" })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("Este navegador no reconoce la voz");
  expect(screen.getByLabelText("Leer las respuestas en voz alta")).toBeDisabled();
  expect(screen.getByText(/no puede leer en voz alta/)).toBeInTheDocument();
});

it("offers to continue past a reached cost cap, and does not read when told not to", async () => {
  let posts = 0;
  const fetchMock = stubApi({
    [TUTOR]: jsonResponse({ subject: "historia", topic: "revolucion-industrial", turns: [] }),
    [`POST ${TUTOR}`]: () =>
      posts++ === 0
        ? sseResponse([["error", { status: 409, detail: "Se ha alcanzado el límite de gasto del día.", code: "cost_cap_reached" }]])
        : sseResponse([["result", answer("¿Qué?", "Esto.[^p1]")]]),
  });
  const speech = fakeSpeech();
  renderScreen({ speech: speech.output, listen: fakeListen().listen });
  await screen.findByText("Todavía no le has preguntado nada sobre este tema.");
  fireEvent.click(screen.getByLabelText("Leer las respuestas en voz alta"));

  fireEvent.change(screen.getByLabelText("Escribe tu pregunta"), { target: { value: "¿Qué?" } });
  fireEvent.keyDown(screen.getByLabelText("Escribe tu pregunta"), { key: "Enter" });
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("límite de gasto");
  expect(screen.getByLabelText("Escribe tu pregunta")).toHaveValue("¿Qué?");

  fireEvent.click(within(alert).getByRole("button", { name: "Continuar igualmente" }));
  expect(await screen.findByText("Esto.[p1]", { exact: false })).toBeInTheDocument();
  const bodies = fetchMock.mock.calls
    .filter((call) => (call[1] as RequestInit | undefined)?.method === "POST")
    .map(bodyOf);
  expect(bodies[1]).toEqual({ question: "¿Qué?", confirm_over_cap: true });
  expect(speech.speak).not.toHaveBeenCalled();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("reports a failing history and a refused question, and goes back", async () => {
  stubApi({
    [TUTOR]: jsonResponse({ detail: "boom" }, 503),
    [`POST ${TUTOR}`]: jsonResponse({ detail: "Todavía no hay apuntes de este tema." }, 409),
  });
  const onClose = vi.fn();
  renderScreen({ speech: fakeSpeech().output, listen: fakeListen().listen, onClose });
  expect(await screen.findByRole("alert")).toHaveTextContent("No se han podido cargar las preguntas anteriores");

  fireEvent.change(screen.getByLabelText("Escribe tu pregunta"), { target: { value: "¿Qué?" } });
  fireEvent.click(screen.getByRole("button", { name: "Preguntar" }));
  await waitFor(() => expect(screen.getByText("Todavía no hay apuntes de este tema.")).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "Continuar igualmente" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "← Volver" }));
  expect(onClose).toHaveBeenCalled();
});
