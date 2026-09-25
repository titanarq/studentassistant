import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import PracticePage from "./PracticePage";
import { describeInterval, readQueue } from "./api";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const TOPIC = "/api/subjects/matematicas/topics/derivadas";
const TOPICS = {
  "/api/subjects/matematicas/topics": jsonResponse({
    subject_id: "matematicas",
    topics: [{ topic_id: "derivadas", subject_id: "matematicas", name: "Derivadas" }],
  }),
};

const CARD = {
  item: {
    key: "flashcards:c0000abcd",
    source: "flashcards",
    prompt: "¿Qué es la derivada?",
    answer: "Un límite.",
    question_type: null,
    options: [],
    explanation: "",
    anchors: ["definicion"],
  },
  state: null,
};
const CHOICE = {
  item: {
    key: "quiz:aaaaaaaaaaaa",
    source: "quiz",
    prompt: "¿Cuánto vale la derivada de x²?",
    answer: "2x",
    question_type: "multiple_choice",
    options: ["2x", "x", "x²"],
    explanation: "Regla de la potencia.",
    anchors: [],
  },
  state: { reviews: 2, interval_days: 6, due: "2026-09-25T10:00:00Z" },
};
const SHORT = {
  item: {
    key: "quiz:bbbbbbbbbbbb",
    source: "quiz",
    prompt: "¿Qué regla se verá el próximo día?",
    answer: "La regla de la cadena",
    question_type: "short_answer",
    options: [],
    explanation: "",
    anchors: ["proximo-dia"],
  },
  state: null,
};

function queue(items: unknown[], overrides: Record<string, unknown> = {}) {
  return {
    now: "2026-09-25T18:00:00Z",
    queue: items,
    counts: { total: 5, due: 1, new: 2, unseen: 3, learned: 2, new_today: 0 },
    next_due: null,
    warnings: [],
    ...overrides,
  };
}

function outcome(rating: string, correct: boolean | null, due: string) {
  return {
    review: { time: "2026-09-25T18:00:00Z", item: "x", source: "quiz", rating, correct },
    state: { reviews: 1, interval_days: 1, due },
  };
}

function renderPage() {
  render(<PracticePage subjectId="matematicas" topicId="derivadas" />);
}

it("reviews a flashcard, sending the student's rating", async () => {
  vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-09-25T18:00:00Z") });
  const sent: unknown[] = [];
  const fetchMock = stubApi({
    ...TOPICS,
    [`${TOPIC}/practice`]: jsonResponse(queue([CARD])),
  });
  const original = fetchMock.getMockImplementation();
  fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      sent.push(JSON.parse(String(init.body)));
      return jsonResponse(outcome("good", null, "2026-09-26T18:00:00Z"));
    }
    return original!(input, init);
  });
  renderPage();

  expect(await screen.findByText("¿Qué es la derivada?")).toBeInTheDocument();
  expect(screen.getByText(/1 para repasar · 2 nuevas · 2 de 5 ya vistas/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Mostrar respuesta" }));
  expect(screen.getByText("Un límite.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "#definicion" })).toHaveAttribute(
    "href",
    "/subjects/matematicas/topics/derivadas/notes#definicion",
  );
  fireEvent.click(screen.getByRole("button", { name: "Bien" }));

  expect(await screen.findByRole("status")).toHaveTextContent("Bien: volverá mañana.");
  expect(sent).toEqual([{ item: "flashcards:c0000abcd", rating: "good" }]);
  expect(screen.getByText(/Has repasado 1 elemento/)).toBeInTheDocument();
});

it("checks a quiz question, rates a right answer and requeues a wrong one", async () => {
  const sent: Record<string, unknown>[] = [];
  const fetchMock = stubApi({ ...TOPICS, [`${TOPIC}/practice`]: jsonResponse(queue([CHOICE, SHORT])) });
  const original = fetchMock.getMockImplementation();
  fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      sent.push(body);
      if (body.item === CHOICE.item.key) return jsonResponse(outcome("easy", true, "2099-01-01T00:00:00Z"));
      return jsonResponse(outcome("again", false, "2026-09-25T18:10:00Z"));
    }
    return original!(input, init);
  });
  renderPage();

  const card = await screen.findByRole("article", { name: "Pregunta" });
  fireEvent.click(within(card).getByLabelText("2x"));
  fireEvent.click(within(card).getByRole("button", { name: "Comprobar" }));
  expect(within(card).getByText("✓ Correcta")).toBeInTheDocument();
  expect(within(card).getByText("Regla de la potencia.")).toBeInTheDocument();
  expect(within(card).queryByRole("button", { name: "Otra vez" })).toBeNull();
  fireEvent.click(within(card).getByRole("button", { name: "Fácil" }));

  expect(await screen.findByText("¿Qué regla se verá el próximo día?")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/Tu respuesta/, { selector: "input" }), {
    target: { value: "la de la cadena" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Comprobar" }));
  expect(screen.getByText(/¿La has acertado\?/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "No" }));
  expect(screen.getByText(/✗ Incorrecta/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Siguiente" }));

  expect(await screen.findByRole("status")).toHaveTextContent("Volverá a salir al final de esta práctica.");
  // The wrong one comes back, asked afresh.
  expect(screen.getByText("¿Qué regla se verá el próximo día?")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Comprobar" })).toBeInTheDocument();
  expect(screen.getByText(/quedan 1/)).toBeInTheDocument();
  expect(sent).toEqual([
    { item: CHOICE.item.key, given: "2x", rating: "easy" },
    { item: SHORT.item.key, given: "la de la cadena", self_assessed: false },
  ]);
});

it("says when the next review is due when nothing is left, and shows warnings", async () => {
  stubApi({
    ...TOPICS,
    [`${TOPIC}/practice`]: jsonResponse(
      queue([], { next_due: "2026-09-27T09:00:00Z", warnings: ["El quiz es de una versión anterior."] }),
    ),
  });
  renderPage();

  expect(await screen.findByText("No tienes nada que repasar ahora.")).toBeInTheDocument();
  expect(screen.getByText(/Próximo repaso: 27 de septiembre de 2026/)).toBeInTheDocument();
  expect(screen.getByRole("note")).toHaveTextContent("El quiz es de una versión anterior.");
});

it("shows a refused review and keeps the item", async () => {
  stubApi({
    ...TOPICS,
    [`${TOPIC}/practice`]: jsonResponse(queue([CARD])),
    [`POST ${TOPIC}/practice/reviews`]: jsonResponse({ detail: "Ya no está." }, 404),
  });
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "Mostrar respuesta" }));
  fireEvent.click(screen.getByRole("button", { name: "Otra vez" }));

  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Ya no está."));
  expect(screen.getByText("¿Qué es la derivada?")).toBeInTheDocument();
});

it("reads the queue leniently and describes intervals", () => {
  const read = readQueue(queue([CARD, { item: { key: "quiz:x", source: "quiz", prompt: "?", answer: "a" } }]));
  expect(read?.queue.map((q) => q.item.key)).toEqual(["flashcards:c0000abcd"]);
  expect(readQueue({ queue: [] })).toBeNull();
  expect(describeInterval(10 / (24 * 60))).toBe("en 10 minutos");
  expect(describeInterval(0.25)).toBe("en 6 horas");
  expect(describeInterval(1)).toBe("mañana");
  expect(describeInterval(6)).toBe("en 6 días");
  expect(describeInterval(90)).toBe("en 3 meses");
});
