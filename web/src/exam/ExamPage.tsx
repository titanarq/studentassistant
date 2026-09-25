import { useCallback, useEffect, useRef, useState } from "react";
import { fetchTopics, topicPath } from "../desk/api";
import { type ActionResult, describeActionFailure } from "../pending/doubts";
import {
  type ExamQuestion,
  type ExamResult,
  fetchExam,
  fetchExamResults,
  formatNumber,
  saveExamResult,
  type StoredExam,
} from "./api";
import "./exam.css";

/**
 * `/subjects/<subject>/topics/<topic>/exam` (#283): correct the mock exam the student sat on
 * paper. Every question shows its statement and "Ver solución y criterios", which reveals the
 * worked solution and the rubric with a points input per criterion (between 0 and its points).
 * The total out of the exam's points is added up as they type; "Guardar corrección" sends it and
 * the backend checks it, computes the score and keeps it in the vault. Earlier corrections are
 * listed below. Generating the exam is done from the topic's study materials.
 */

const DATE_FORMAT = new Intl.DateTimeFormat("es-ES", { dateStyle: "long", timeStyle: "short" });

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : DATE_FORMAT.format(date);
}

function pointsText(value: number): string {
  return `${formatNumber(value)} ${value === 1 ? "punto" : "puntos"}`;
}

function key(question: ExamQuestion, index: number): string {
  return `${question.id}:${index}`;
}

/** The points typed for a criterion: empty is 0; `null` when not a number in `[0, max]`. */
export function parsePoints(raw: string | undefined, max: number): number | null {
  const text = (raw ?? "").trim().replace(",", ".");
  if (text === "") return 0;
  const value = Number(text);
  if (!Number.isFinite(value) || value < 0 || value > max + 1e-9) return null;
  return value;
}

function questionScore(question: ExamQuestion, values: Record<string, string>): number | null {
  let sum = 0;
  for (const [index, criterion] of question.criteria.entries()) {
    const value = parsePoints(values[key(question, index)], criterion.points);
    if (value === null) return null;
    sum += value;
  }
  return Math.min(sum, question.points);
}

function QuestionCard({
  question,
  values,
  notesHref,
  onChange,
}: {
  question: ExamQuestion;
  values: Record<string, string>;
  notesHref: string;
  onChange: (key: string, value: string) => void;
}) {
  const [revealed, setRevealed] = useState(false);
  const score = questionScore(question, values);
  return (
    <li className="exam-question">
      <fieldset aria-label={`Pregunta ${question.number}`}>
        <legend>
          Pregunta {question.number} <span className="exam-points">({pointsText(question.points)})</span>
        </legend>
        <p className="exam-statement">{question.statement}</p>
        <button type="button" aria-expanded={revealed} onClick={() => setRevealed((value) => !value)}>
          {revealed ? "Ocultar solución y criterios" : "Ver solución y criterios"}
        </button>
        {revealed && (
          <div className="exam-reveal" role="group" aria-label={`Solución de la pregunta ${question.number}`}>
            <h3>Solución</h3>
            <p className="exam-solution">{question.solution}</p>
            <h3>Criterios de corrección</h3>
            <ul className="exam-criteria">
              {question.criteria.map((criterion, index) => {
                const id = key(question, index);
                const invalid = parsePoints(values[id], criterion.points) === null;
                return (
                  <li key={id}>
                    <label>
                      {criterion.criterion} <span className="exam-points">(máx. {formatNumber(criterion.points)})</span>{" "}
                      <input
                        type="text"
                        inputMode="decimal"
                        value={values[id] ?? ""}
                        placeholder="0"
                        aria-invalid={invalid}
                        onChange={(event) => onChange(id, event.target.value)}
                      />
                    </label>
                    {invalid && (
                      <span className="exam-invalid"> Entre 0 y {formatNumber(criterion.points)}.</span>
                    )}
                  </li>
                );
              })}
            </ul>
            {question.anchors.length > 0 && (
              <p className="exam-sources">
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
        <p className="exam-subtotal">
          {score === null
            ? "Revisa los puntos de esta pregunta."
            : `${formatNumber(score)} de ${formatNumber(question.points)}`}
        </p>
      </fieldset>
    </li>
  );
}

export default function ExamPage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [topicName, setTopicName] = useState(topicId);
  const [exam, setExam] = useState<ActionResult<StoredExam> | null>(null);
  const [history, setHistory] = useState<ExamResult[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<ActionResult<ExamResult> | null>(null);
  const reads = useRef(0);

  const load = useCallback(async () => {
    const read = ++reads.current;
    const [result, results] = await Promise.all([
      fetchExam(subjectId, topicId),
      fetchExamResults(subjectId, topicId),
    ]);
    if (read !== reads.current) return;
    setExam(result);
    setHistory(results.kind === "ok" ? results.value : []);
    setValues({});
    setSaved(null);
  }, [subjectId, topicId]);

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
  const stored = exam?.kind === "ok" ? exam.value : null;
  const questions = stored?.questions ?? [];
  const scores = questions.map((q) => questionScore(q, values));
  const valid = scores.every((s) => s !== null);
  const score = scores.reduce<number>((sum, s) => sum + (s ?? 0), 0);
  const total = questions.reduce((sum, q) => sum + q.points, 0);
  const percentage = total > 0 ? Math.round((1000 * score) / total) / 10 : 0;

  async function save() {
    if (stored === null || !valid) return;
    setSaving(true);
    const result = await saveExamResult(subjectId, topicId, {
      built_at: stored.built_at,
      questions: questions.map((q) => ({
        question: q.id,
        awarded: q.criteria.map((c, index) => parsePoints(values[key(q, index)], c.points) ?? 0),
      })),
    });
    setSaving(false);
    setSaved(result);
    if (result.kind === "ok") setHistory((previous) => [...previous, result.value]);
  }

  function change(id: string, value: string) {
    setValues((previous) => ({ ...previous, [id]: value }));
    setSaved(null);
  }

  return (
    <main className="exam-page">
      <p>
        <a href={base}>← Tema {topicName}</a>
      </p>
      <h1>Corregir examen de {topicName}</h1>
      {exam === null && <p>Cargando el examen…</p>}
      {exam !== null && exam.kind !== "ok" && (
        <>
          <p>{describeActionFailure(exam)}</p>
          <p>
            Genera el examen desde el <a href={base}>material de estudio del tema</a>.
          </p>
        </>
      )}
      {stored !== null && (
        <>
          <p className="exam-meta">
            {questions.length} {questions.length === 1 ? "pregunta" : "preguntas"} · {pointsText(total)}
            {stored.durationMinutes !== null && ` · ${stored.durationMinutes} minutos`}
            {stored.notes_version !== null && ` · de los apuntes v${stored.notes_version}`}
          </p>
          {stored.stale && (
            <p className="exam-warning" role="note">
              {stored.stale_reason ?? "Los apuntes han cambiado desde que se generó este examen."} Puedes
              corregirlo igualmente o generar uno nuevo desde el material de estudio.
            </p>
          )}
          <p>Haz el examen en papel y, después, puntúa cada criterio con la solución delante.</p>
          <ol className="exam-questions">
            {questions.map((question) => (
              <QuestionCard
                key={question.id}
                question={question}
                values={values}
                notesHref={`${base}/notes`}
                onChange={change}
              />
            ))}
          </ol>
          <section className="exam-score" aria-label="Resultado">
            <p>
              Total: {formatNumber(score)} de {formatNumber(total)} ({formatNumber(percentage)} %)
            </p>
            {saved?.kind === "ok" ? (
              <p role="status">
                Corrección guardada: {formatNumber(saved.value.score)} de {formatNumber(saved.value.total)} (
                {formatNumber(saved.value.percentage)} %).
              </p>
            ) : (
              <button type="button" onClick={() => void save()} disabled={saving || !valid || questions.length === 0}>
                {saving ? "Guardando…" : "Guardar corrección"}
              </button>
            )}
            {saved !== null && saved.kind !== "ok" && <p role="alert">{describeActionFailure(saved)}</p>}
          </section>
        </>
      )}
      {history.length > 0 && (
        <section aria-label="Correcciones anteriores">
          <h2>Correcciones anteriores</h2>
          <ul>
            {[...history]
              .reverse()
              .slice(0, 10)
              .map((entry, index) => (
                <li key={`${entry.time}-${index}`}>
                  {formatTime(entry.time)}: {formatNumber(entry.score)} de {formatNumber(entry.total)} (
                  {formatNumber(entry.percentage)} %)
                </li>
              ))}
          </ul>
        </section>
      )}
    </main>
  );
}
