import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { fetchTopics, topicPath } from "../desk/api";
import { type ActionResult, describeActionFailure } from "../pending/doubts";
import {
  DIFFICULTY_LABELS,
  fetchQuiz,
  fetchQuizResults,
  generateQuiz,
  normalizeAnswer,
  type QuizDifficulty,
  type QuizQuestion,
  type QuizResult,
  saveQuizResult,
  type StoredQuiz,
} from "./api";
import "./quiz.css";

/**
 * `/subjects/<subject>/topics/<topic>/quiz` (#75): take the topic's quiz. The student answers
 * every question (options for multiple choice and true/false, a text for a short answer), then
 * "Corregir" shows each verdict, the right answer, its explanation and links to the note sections
 * it comes from. A short answer that does not match the expected one is judged by the student
 * ("¿La has acertado?"). "Guardar resultado" sends the attempt, which the backend grades and keeps
 * in the vault; earlier attempts are listed below. A form generates the quiz (or a new one) with a
 * number of questions and a difficulty.
 */

type Phase = "answering" | "corrected";

const DATE_FORMAT = new Intl.DateTimeFormat("es-ES", { dateStyle: "long", timeStyle: "short" });

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : DATE_FORMAT.format(date);
}

/** `true`/`false` when the answer can be graded alone; `null` for a short answer to self-assess. */
export function autoVerdict(question: QuizQuestion, given: string | undefined): boolean | null {
  const value = (given ?? "").trim();
  if (value === "") return false;
  const match = normalizeAnswer(value) === normalizeAnswer(question.answer);
  if (match || question.type !== "short_answer") return match;
  return null;
}

function GenerateForm({
  subjectId,
  topicId,
  hasQuiz,
  onGenerated,
}: {
  subjectId: string;
  topicId: string;
  hasQuiz: boolean;
  onGenerated: () => void;
}) {
  const [size, setSize] = useState(10);
  const [difficulty, setDifficulty] = useState<QuizDifficulty>("mixed");
  const [generating, setGenerating] = useState(false);
  const [failure, setFailure] = useState<Exclude<ActionResult<true>, { kind: "ok" }> | null>(null);

  async function generate(confirmOverCap: boolean) {
    setGenerating(true);
    setFailure(null);
    const result = await generateQuiz(subjectId, topicId, { size, difficulty }, confirmOverCap);
    setGenerating(false);
    if (result.kind === "ok") onGenerated();
    else setFailure(result);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void generate(false);
  }

  return (
    <form className="quiz-generate" aria-label="Generar un quiz" onSubmit={submit}>
      <h2>{hasQuiz ? "Generar un quiz nuevo" : "Generar el quiz"}</h2>
      <label>
        Número de preguntas{" "}
        <input
          type="number"
          min={1}
          max={30}
          value={size}
          onChange={(event) => setSize(Math.min(30, Math.max(1, Number(event.target.value) || 1)))}
        />
      </label>{" "}
      <label>
        Dificultad{" "}
        <select value={difficulty} onChange={(event) => setDifficulty(event.target.value as QuizDifficulty)}>
          {(Object.keys(DIFFICULTY_LABELS) as QuizDifficulty[]).map((value) => (
            <option key={value} value={value}>
              {DIFFICULTY_LABELS[value]}
            </option>
          ))}
        </select>
      </label>{" "}
      <button type="submit" disabled={generating}>
        {generating ? "Generando el quiz…" : "Generar quiz"}
      </button>
      {failure !== null && (
        <p role="alert">
          {describeActionFailure(failure)}{" "}
          {failure.kind === "refused" && failure.overCap && (
            <button type="button" disabled={generating} onClick={() => void generate(true)}>
              Generar igualmente
            </button>
          )}
        </p>
      )}
    </form>
  );
}

function QuestionCard({
  number,
  question,
  given,
  phase,
  assessed,
  notesHref,
  onAnswer,
  onAssess,
}: {
  number: number;
  question: QuizQuestion;
  given: string | undefined;
  phase: Phase;
  assessed: boolean | undefined;
  notesHref: string;
  onAnswer: (value: string) => void;
  onAssess: (value: boolean) => void;
}) {
  const corrected = phase === "corrected";
  const auto = autoVerdict(question, given);
  const verdict = auto ?? assessed ?? null;
  const name = `pregunta-${question.id}`;
  return (
    <li className="quiz-question">
      <fieldset aria-label={`Pregunta ${number}`}>
        <legend>
          Pregunta {number} <span className="quiz-difficulty">· {DIFFICULTY_LABELS[question.difficulty]}</span>
        </legend>
        <p className="quiz-statement">{question.question}</p>
        {question.type === "short_answer" ? (
          <label>
            Tu respuesta{" "}
            <input
              type="text"
              name={name}
              value={given ?? ""}
              disabled={corrected}
              onChange={(event) => onAnswer(event.target.value)}
            />
          </label>
        ) : (
          <ul className="quiz-options">
            {question.options.map((option) => (
              <li key={option}>
                <label>
                  <input
                    type="radio"
                    name={name}
                    value={option}
                    checked={given === option}
                    disabled={corrected}
                    onChange={() => onAnswer(option)}
                  />{" "}
                  {option}
                </label>
              </li>
            ))}
          </ul>
        )}
        {corrected && (
          <div className="quiz-feedback" role="group" aria-label={`Corrección de la pregunta ${number}`}>
            {verdict === null ? (
              <p>
                La respuesta esperada es: <strong>{question.answer}</strong>. ¿La has acertado?{" "}
                <button type="button" onClick={() => onAssess(true)}>
                  Sí
                </button>{" "}
                <button type="button" onClick={() => onAssess(false)}>
                  No
                </button>
              </p>
            ) : verdict ? (
              <p className="quiz-right">✓ Correcta</p>
            ) : (
              <p className="quiz-wrong">
                ✗ Incorrecta. La respuesta es: <strong>{question.answer}</strong>
              </p>
            )}
            {question.explanation !== "" && <p>{question.explanation}</p>}
            {question.anchors.length > 0 && (
              <p className="quiz-sources">
                En los apuntes:{" "}
                {question.anchors.map((anchor, index) => (
                  <span key={anchor}>
                    {index > 0 && ", "}
                    <a href={`${notesHref}#${encodeURIComponent(anchor)}`}>#{anchor}</a>
                  </span>
                ))}
              </p>
            )}
          </div>
        )}
      </fieldset>
    </li>
  );
}

export default function QuizPage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [topicName, setTopicName] = useState(topicId);
  const [quiz, setQuiz] = useState<ActionResult<StoredQuiz> | null>(null);
  const [history, setHistory] = useState<QuizResult[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [assessed, setAssessed] = useState<Record<string, boolean>>({});
  const [phase, setPhase] = useState<Phase>("answering");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<ActionResult<QuizResult> | null>(null);
  const startedAt = useRef(Date.now());
  const reads = useRef(0);

  const restart = useCallback(() => {
    setAnswers({});
    setAssessed({});
    setPhase("answering");
    setSaved(null);
    startedAt.current = Date.now();
  }, []);

  const load = useCallback(async () => {
    const read = ++reads.current;
    const [result, results] = await Promise.all([
      fetchQuiz(subjectId, topicId),
      fetchQuizResults(subjectId, topicId),
    ]);
    if (read !== reads.current) return;
    setQuiz(result);
    setHistory(results.kind === "ok" ? results.value : []);
    restart();
  }, [subjectId, topicId, restart]);

  useEffect(() => {
    let cancelled = false;
    fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (!cancelled && topic) setTopicName(topic.name);
    });
    void load();
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, load]);

  const base = topicPath(subjectId, topicId);
  const stored = quiz?.kind === "ok" ? quiz.value : null;
  const questions = stored?.questions ?? [];
  const verdicts = questions.map((q) => autoVerdict(q, answers[q.id]) ?? assessed[q.id] ?? null);
  const toAssess = verdicts.filter((v) => v === null).length;
  const correct = verdicts.filter((v) => v === true).length;

  async function save() {
    if (stored === null) return;
    setSaving(true);
    const result = await saveQuizResult(subjectId, topicId, {
      built_at: stored.built_at,
      answers: questions.map((q) => {
        const given = (answers[q.id] ?? "").trim();
        const answer: { question: string; given: string | null; self_assessed?: boolean } = {
          question: q.id,
          given: given === "" ? null : given,
        };
        if (q.id in assessed) answer.self_assessed = assessed[q.id];
        return answer;
      }),
      duration_seconds: Math.max(0, Math.round((Date.now() - startedAt.current) / 1000)),
    });
    setSaving(false);
    setSaved(result);
    if (result.kind === "ok") setHistory((previous) => [...previous, result.value]);
  }

  return (
    <main className="quiz-page">
      <p>
        <a href={base}>← Tema {topicName}</a>
      </p>
      <h1>Quiz de {topicName}</h1>
      {quiz === null && <p>Cargando el quiz…</p>}
      {quiz !== null && quiz.kind !== "ok" && <p>{describeActionFailure(quiz)}</p>}
      {stored !== null && (
        <>
          <p className="quiz-meta">
            {questions.length} preguntas · dificultad {DIFFICULTY_LABELS[stored.difficulty].toLowerCase()}
            {stored.notes_version !== null && ` · de los apuntes v${stored.notes_version}`}
          </p>
          {stored.stale && (
            <p className="quiz-warning" role="note">
              {stored.stale_reason ?? "Los apuntes han cambiado desde que se generó este quiz."} Puedes generar
              uno nuevo abajo.
            </p>
          )}
          <ol className="quiz-questions">
            {questions.map((question, index) => (
              <QuestionCard
                key={question.id}
                number={index + 1}
                question={question}
                given={answers[question.id]}
                phase={phase}
                assessed={assessed[question.id]}
                notesHref={`${base}/notes`}
                onAnswer={(value) => setAnswers((previous) => ({ ...previous, [question.id]: value }))}
                onAssess={(value) => setAssessed((previous) => ({ ...previous, [question.id]: value }))}
              />
            ))}
          </ol>
          {phase === "answering" ? (
            <button type="button" onClick={() => setPhase("corrected")} disabled={questions.length === 0}>
              Corregir
            </button>
          ) : (
            <section className="quiz-score" aria-label="Resultado">
              <p>
                Aciertos: {correct} de {questions.length}
                {toAssess > 0 && ` (te falta valorar ${toAssess === 1 ? "1 respuesta" : `${toAssess} respuestas`})`}
              </p>
              {saved?.kind === "ok" ? (
                <p role="status">
                  Resultado guardado: {saved.value.correct} de {saved.value.total}.{" "}
                  <button type="button" onClick={restart}>
                    Repetir el quiz
                  </button>
                </p>
              ) : (
                <button type="button" onClick={() => void save()} disabled={saving || toAssess > 0}>
                  {saving ? "Guardando…" : "Guardar resultado"}
                </button>
              )}
              {saved !== null && saved.kind !== "ok" && <p role="alert">{describeActionFailure(saved)}</p>}
            </section>
          )}
        </>
      )}
      {history.length > 0 && (
        <section aria-label="Intentos anteriores">
          <h2>Intentos anteriores</h2>
          <ul>
            {[...history]
              .reverse()
              .slice(0, 10)
              .map((entry, index) => (
                <li key={`${entry.time}-${index}`}>
                  {formatTime(entry.time)}: {entry.correct} de {entry.total}
                </li>
              ))}
          </ul>
        </section>
      )}
      {quiz !== null && (
        <GenerateForm
          subjectId={subjectId}
          topicId={topicId}
          hasQuiz={stored !== null}
          onGenerated={() => void load()}
        />
      )}
    </main>
  );
}
