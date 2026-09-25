import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import QuizPage from "./QuizPage";
import { normalizeAnswer, readStoredQuiz } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

const TOPIC = "/api/subjects/matematicas/topics/derivadas";
const BUILT_AT = "2026-09-25T18:00:00Z";
const TOPICS = {
  "/api/subjects/matematicas/topics": jsonResponse({
    subject_id: "matematicas",
    topics: [{ topic_id: "derivadas", subject_id: "matematicas", name: "Derivadas" }],
  }),
};

function storedQuiz(overrides: Record<string, unknown> = {}) {
  return {
    quiz: {
      title: "Quiz: Derivadas",
      difficulty: "mixed",
      questions: [
        {
          id: "q1",
          type: "multiple_choice",
          difficulty: "easy",
          question: "¿Qué es la derivada?",
          options: ["Un límite", "Una integral", "Una suma"],
          answer: "Un límite",
          explanation: "Es el límite del cociente incremental.",
          anchors: ["definicion"],
        },
        {
          id: "q2",
          type: "true_false",
          difficulty: "medium",
          question: "La derivada se escribe f'(x).",
          options: ["Verdadero", "Falso"],
          answer: "Verdadero",
          explanation: "",
          anchors: [],
        },
        {
          id: "q3",
          type: "short_answer",
          difficulty: "hard",
          question: "¿Qué regla se verá el próximo día?",
          options: [],
          answer: "La regla de la cadena",
          explanation: "",
          anchors: ["proximo-dia"],
        },
      ],
    },
    built_at: BUILT_AT,
    notes_version: 2,
    warnings: [],
    stale: false,
    stale_reason: null,
    ...overrides,
  };
}

function renderPage() {
  render(<QuizPage subjectId="matematicas" topicId="derivadas" />);
}

it("takes the quiz, corrects it with self-assessment and saves the result", async () => {
  let sent: unknown = null;
  const fetchMock = stubApi({});
  fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      sent = JSON.parse(String(init.body));
      return jsonResponse({ time: "2026-09-25T18:05:00Z", total: 3, correct: 2 });
    }
    if (input === `${TOPIC}/quiz`) return jsonResponse(storedQuiz());
    if (input === `${TOPIC}/quiz/results`) return jsonResponse([{ time: "2026-09-24T10:00:00Z", total: 3, correct: 1 }]);
    return TOPICS["/api/subjects/matematicas/topics"].clone();
  });
  renderPage();

  expect(await screen.findByRole("heading", { name: "Quiz de Derivadas" })).toBeInTheDocument();
  expect(screen.getByText(/3 preguntas · dificultad variada · de los apuntes v2/)).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Intentos anteriores" })).toHaveTextContent("1 de 3");

  const first = screen.getByRole("group", { name: "Pregunta 1" });
  fireEvent.click(within(first).getByLabelText("Un límite"));
  fireEvent.click(within(screen.getByRole("group", { name: "Pregunta 2" })).getByLabelText("Falso"));
  fireEvent.change(within(screen.getByRole("group", { name: "Pregunta 3" })).getByLabelText("Tu respuesta"), {
    target: { value: "la cadena" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Corregir" }));

  expect(screen.getByRole("group", { name: "Corrección de la pregunta 1" })).toHaveTextContent("✓ Correcta");
  expect(screen.getByRole("group", { name: "Corrección de la pregunta 1" })).toHaveTextContent(
    "Es el límite del cociente incremental.",
  );
  expect(within(first).getByRole("link", { name: "#definicion" })).toHaveAttribute(
    "href",
    "/subjects/matematicas/topics/derivadas/notes#definicion",
  );
  expect(screen.getByRole("group", { name: "Corrección de la pregunta 2" })).toHaveTextContent(
    "✗ Incorrecta. La respuesta es: Verdadero",
  );
  const third = screen.getByRole("group", { name: "Corrección de la pregunta 3" });
  expect(third).toHaveTextContent("¿La has acertado?");
  expect(screen.getByRole("button", { name: "Guardar resultado" })).toBeDisabled();
  expect(screen.getByRole("region", { name: "Resultado" })).toHaveTextContent("Aciertos: 1 de 3 (te falta valorar 1 respuesta)");

  fireEvent.click(within(third).getByRole("button", { name: "Sí" }));
  expect(third).toHaveTextContent("✓ Correcta");
  expect(screen.getByRole("region", { name: "Resultado" })).toHaveTextContent("Aciertos: 2 de 3");
  fireEvent.click(screen.getByRole("button", { name: "Guardar resultado" }));

  expect(await screen.findByRole("status")).toHaveTextContent("Resultado guardado: 2 de 3.");
  expect(sent).toMatchObject({
    built_at: BUILT_AT,
    answers: [
      { question: "q1", given: "Un límite" },
      { question: "q2", given: "Falso" },
      { question: "q3", given: "la cadena", self_assessed: true },
    ],
  });
  expect(screen.getByRole("region", { name: "Intentos anteriores" }).querySelectorAll("li")).toHaveLength(2);

  fireEvent.click(screen.getByRole("button", { name: "Repetir el quiz" }));
  expect(screen.getByRole("button", { name: "Corregir" })).toBeInTheDocument();
  expect(within(screen.getByRole("group", { name: "Pregunta 1" })).getByLabelText("Un límite")).not.toBeChecked();
});

it("offers to generate the quiz when there is none, then shows it", async () => {
  let generated = false;
  let body: unknown = null;
  const fetchMock = stubApi({});
  fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      body = JSON.parse(String(init.body));
      generated = true;
      return jsonResponse({ kind: "quiz", files: [] });
    }
    if (input === `${TOPIC}/quiz`) {
      return generated
        ? jsonResponse(storedQuiz({ stale: true, stale_reason: "Los apuntes han cambiado." }))
        : jsonResponse({ detail: "Todavía no hay quiz de este tema: genéralo primero." }, 404);
    }
    if (input === `${TOPIC}/quiz/results`) return jsonResponse([]);
    return jsonResponse({}, 503);
  });
  renderPage();

  expect(await screen.findByText("Todavía no hay quiz de este tema: genéralo primero.")).toBeInTheDocument();
  const form = screen.getByRole("form", { name: "Generar un quiz" });
  fireEvent.change(within(form).getByLabelText("Número de preguntas"), { target: { value: "5" } });
  fireEvent.change(within(form).getByLabelText("Dificultad"), { target: { value: "hard" } });
  fireEvent.click(within(form).getByRole("button", { name: "Generar quiz" }));

  expect(await screen.findByRole("group", { name: "Pregunta 1" })).toBeInTheDocument();
  expect(body).toEqual({ options: { size: 5, difficulty: "hard" }, confirm_over_cap: false });
  expect(fetchMock).toHaveBeenCalledWith(`${TOPIC}/generated/quiz`, expect.objectContaining({ method: "POST" }));
  expect(screen.getByRole("note")).toHaveTextContent("Los apuntes han cambiado.");
  expect(screen.getByRole("heading", { name: "Generar un quiz nuevo" })).toBeInTheDocument();
});

it("asks to confirm when the cost cap is reached", async () => {
  const posts: unknown[] = [];
  const fetchMock = stubApi({});
  fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push(JSON.parse(String(init.body)));
      return posts.length === 1
        ? jsonResponse({ detail: "Has llegado al tope de gasto.", code: "cost_cap_reached" }, 409)
        : jsonResponse({ kind: "quiz" });
    }
    if (input === `${TOPIC}/quiz`) return jsonResponse({ detail: "Todavía no hay quiz." }, 404);
    if (input === `${TOPIC}/quiz/results`) return jsonResponse([]);
    return jsonResponse({}, 503);
  });
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "Generar quiz" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Has llegado al tope de gasto.");
  fireEvent.click(screen.getByRole("button", { name: "Generar igualmente" }));
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[1]).toMatchObject({ confirm_over_cap: true });
});

it("reads the quiz leniently and normalizes answers like the backend", () => {
  const quiz = readStoredQuiz(
    storedQuiz({
      quiz: {
        title: "Q",
        difficulty: "raro",
        questions: [{ id: "x", type: "otro", question: "?", answer: "a" }, storedQuiz().quiz.questions[0]],
      },
    }),
  );
  expect(quiz?.difficulty).toBe("mixed");
  expect(quiz?.questions.map((q) => q.id)).toEqual(["q1"]);
  expect(readStoredQuiz({ quiz: {} })).toBeNull();
  expect(normalizeAnswer("  La Regla  de la CADENA. ")).toBe("la regla de la cadena");
  expect(normalizeAnswer("¿Límite?")).toBe("limite");
});
