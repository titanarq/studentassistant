import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import ExamPage, { parsePoints } from "./ExamPage";
import { formatNumber, readResults, readStoredExam } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

const TOPIC = "/api/subjects/matematicas/topics/derivadas";
const BUILT_AT = "2026-09-25T18:00:00Z";
const TOPICS = jsonResponse({
  subject_id: "matematicas",
  topics: [{ topic_id: "derivadas", subject_id: "matematicas", name: "Derivadas" }],
});

function storedExam(overrides: Record<string, unknown> = {}) {
  return {
    exam: {
      title: "Derivadas",
      instructions: "Justifica cada paso.",
      duration_minutes: 60,
      total_points: 10,
      exercises: [],
      questions: [
        {
          id: "p1",
          number: 1,
          statement: "Define la derivada de f en a.",
          difficulty: "media",
          points: 4,
          solution: "Es el límite del cociente incremental.",
          rubric: [
            { criterion: "Escribe el cociente incremental", points: 2 },
            { criterion: "Toma el límite cuando h → 0", points: 2 },
          ],
          anchors: ["definicion"],
        },
        {
          id: "p2",
          number: 2,
          statement: "¿Qué regla se verá el próximo día?",
          difficulty: "baja",
          points: 6,
          solution: "La regla de la cadena.",
          rubric: [],
          anchors: [],
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

function stub(exam: Response, posted: unknown[] = [], answer?: Response) {
  const fetchMock = stubApi({});
  fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posted.push(JSON.parse(String(init.body)));
      return (answer ?? jsonResponse({ time: "2026-09-25T19:00:00Z", score: 7.5, total: 10, percentage: 75 })).clone();
    }
    if (input === `${TOPIC}/exam`) return exam.clone();
    if (input === `${TOPIC}/exam/results`) {
      return jsonResponse([{ time: "2026-09-24T10:00:00Z", score: 4, total: 10, percentage: 40 }]);
    }
    return TOPICS.clone();
  });
  return fetchMock;
}

function renderPage() {
  render(<ExamPage subjectId="matematicas" topicId="derivadas" />);
}

it("corrects the exam criterion by criterion and saves the result", async () => {
  const posted: unknown[] = [];
  stub(jsonResponse(storedExam()), posted);
  renderPage();

  expect(await screen.findByRole("heading", { name: "Corregir examen de Derivadas" })).toBeInTheDocument();
  expect(screen.getByText(/2 preguntas · 10 puntos · 60 minutos · de los apuntes v2/)).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Correcciones anteriores" })).toHaveTextContent("4 de 10 (40 %)");

  const first = screen.getByRole("group", { name: "Pregunta 1" });
  expect(first).toHaveTextContent("Define la derivada de f en a.");
  expect(first).toHaveTextContent("(4 puntos)");
  expect(within(first).queryByText("Es el límite del cociente incremental.")).toBeNull();
  fireEvent.click(within(first).getByRole("button", { name: "Ver solución y criterios" }));
  const reveal = screen.getByRole("group", { name: "Solución de la pregunta 1" });
  expect(reveal).toHaveTextContent("Es el límite del cociente incremental.");
  expect(within(reveal).getByRole("link", { name: "#definicion" })).toHaveAttribute(
    "href",
    "/subjects/matematicas/topics/derivadas/notes#definicion",
  );
  fireEvent.change(within(reveal).getByLabelText(/Escribe el cociente incremental/), { target: { value: "2" } });
  fireEvent.change(within(reveal).getByLabelText(/Toma el límite/), { target: { value: "1,5" } });
  expect(first).toHaveTextContent("3,5 de 4");

  const second = screen.getByRole("group", { name: "Pregunta 2" });
  fireEvent.click(within(second).getByRole("button", { name: "Ver solución y criterios" }));
  const whole = within(second).getByLabelText(/Pregunta completa/);
  fireEvent.change(whole, { target: { value: "7" } });
  expect(whole).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByRole("button", { name: "Guardar corrección" })).toBeDisabled();
  fireEvent.change(whole, { target: { value: "4" } });
  expect(screen.getByRole("region", { name: "Resultado" })).toHaveTextContent("Total: 7,5 de 10 (75 %)");

  fireEvent.click(screen.getByRole("button", { name: "Guardar corrección" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Corrección guardada: 7,5 de 10 (75 %).");
  expect(posted).toEqual([
    {
      built_at: BUILT_AT,
      questions: [
        { question: "p1", awarded: [2, 1.5] },
        { question: "p2", awarded: [4] },
      ],
    },
  ]);
  expect(screen.getByRole("region", { name: "Correcciones anteriores" }).querySelectorAll("li")).toHaveLength(2);
});

it("shows the backend's refusal when the exam changed", async () => {
  stub(
    jsonResponse(storedExam({ stale: true, stale_reason: "Los apuntes han cambiado." })),
    [],
    jsonResponse({ detail: "El examen ha cambiado mientras lo corregías (se ha vuelto a generar): corrige el nuevo." }, 409),
  );
  renderPage();

  expect(await screen.findByRole("note")).toHaveTextContent("Los apuntes han cambiado.");
  fireEvent.click(screen.getByRole("button", { name: "Guardar corrección" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("El examen ha cambiado");
});

it("says there is no exam yet and points to the study materials", async () => {
  stub(jsonResponse({ detail: "Todavía no hay examen que corregir en este tema." }, 404));
  renderPage();

  expect(await screen.findByText("Todavía no hay examen que corregir en este tema.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "material de estudio del tema" })).toHaveAttribute(
    "href",
    "/subjects/matematicas/topics/derivadas",
  );
  expect(screen.queryByRole("button", { name: "Guardar corrección" })).toBeNull();
});

it("reads the exam leniently and parses points in Spanish", () => {
  const exam = readStoredExam(
    storedExam({
      exam: {
        title: "E",
        questions: [
          { id: "x" },
          { id: "p1", statement: "Sin puntos", rubric: [{ criterion: "a", points: 1 }, { criterion: "b", points: 2 }] },
        ],
      },
    }),
  );
  expect(exam?.questions.map((q) => [q.id, q.points, q.criteria.length])).toEqual([["p1", 3, 2]]);
  expect(readStoredExam({ exam: {} })).toBeNull();
  expect(readResults([{ time: "t", score: 1, total: 2, percentage: 50 }, { time: "t" }])).toHaveLength(1);
  expect(formatNumber(2.5)).toBe("2,5");
  expect(formatNumber(10)).toBe("10");
  expect(parsePoints("", 2)).toBe(0);
  expect(parsePoints("1,25", 2)).toBe(1.25);
  expect(parsePoints("3", 2)).toBeNull();
  expect(parsePoints("-1", 2)).toBeNull();
  expect(parsePoints("abc", 2)).toBeNull();
});
