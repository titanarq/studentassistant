import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { fetchTopics, topicPath } from "../desk/api";
import { type ActionResult, describeActionFailure } from "../pending/doubts";
import { normalizeAnswer } from "../quiz/api";
import {
  describeInterval,
  fetchPractice,
  type PracticeAnswer,
  type PracticeItem,
  type PracticeQueue,
  type QueuedItem,
  RATING_LABELS,
  type Rating,
  sendReview,
} from "./api";
import "./practice.css";

/**
 * `/subjects/<subject>/topics/<topic>/practice` (#81): practise the topic's flashcards and quiz
 * questions with spaced repetition. The backend says what is due (and the new items of the day);
 * the page shows one item at a time. A flashcard shows its front, "Mostrar respuesta" its back,
 * and the student rates it ("Otra vez", "Difícil", "Bien", "Fácil"). A quiz question is answered
 * and checked; a short answer that does not match is judged by the student; a right answer is then
 * rated ("Difícil", "Bien", "Fácil"), a wrong one just goes on. Each review is sent at once (the
 * backend grades it, keeps it in the vault and schedules the item); an item rated "Otra vez"
 * comes back at the end of the session. When nothing is left, the page says when the next review
 * is due.
 */

type Phase = "asking" | "revealed";

const DATE_FORMAT = new Intl.DateTimeFormat("es-ES", { dateStyle: "long", timeStyle: "short" });
const DAY_MS = 24 * 60 * 60 * 1000;

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : DATE_FORMAT.format(date);
}

function untilDue(due: string | null): string | null {
  if (due === null) return null;
  const time = Date.parse(due);
  if (Number.isNaN(time)) return null;
  return describeInterval(Math.max(0, time - Date.now()) / DAY_MS);
}

/** `true`/`false` when the answer can be checked alone; `null` for a short answer to judge. */
export function checkAnswer(item: PracticeItem, given: string): boolean | null {
  const value = given.trim();
  if (value === "") return false;
  const match = normalizeAnswer(value) === normalizeAnswer(item.answer);
  if (match || item.question_type !== "short_answer") return match;
  return null;
}

function SourceLinks({ anchors, notesHref }: { anchors: string[]; notesHref: string }) {
  if (anchors.length === 0) return null;
  return (
    <p className="practice-sources">
      En los apuntes:{" "}
      {anchors.map((anchor, index) => (
        <span key={anchor}>
          {index > 0 && ", "}
          <a href={`${notesHref}#${encodeURIComponent(anchor)}`}>#{anchor}</a>
        </span>
      ))}
    </p>
  );
}

function RatingButtons({
  ratings,
  disabled,
  onRate,
}: {
  ratings: Rating[];
  disabled: boolean;
  onRate: (rating: Rating) => void;
}) {
  return (
    <p className="practice-ratings" role="group" aria-label="¿Cómo te ha ido?">
      {ratings.map((rating) => (
        <button key={rating} type="button" disabled={disabled} onClick={() => onRate(rating)}>
          {RATING_LABELS[rating]}
        </button>
      ))}
    </p>
  );
}

function ItemCard({
  queued,
  notesHref,
  sending,
  onReview,
}: {
  queued: QueuedItem;
  notesHref: string;
  sending: boolean;
  onReview: (answer: Omit<PracticeAnswer, "item">) => void;
}) {
  const { item } = queued;
  const [phase, setPhase] = useState<Phase>("asking");
  const [given, setGiven] = useState("");
  const [assessed, setAssessed] = useState<boolean | null>(null);
  const revealed = phase === "revealed";
  const isQuiz = item.source === "quiz";
  const verdict = isQuiz && revealed ? (checkAnswer(item, given) ?? assessed) : null;

  function check(event: FormEvent) {
    event.preventDefault();
    setPhase("revealed");
  }

  function quizReview(rating?: Rating) {
    const answer: Omit<PracticeAnswer, "item"> = { given: given.trim() === "" ? null : given.trim() };
    if (assessed !== null && checkAnswer(item, given) === null) answer.self_assessed = assessed;
    if (rating !== undefined) answer.rating = rating;
    onReview(answer);
  }

  return (
    <article className="practice-card" aria-label={isQuiz ? "Pregunta" : "Tarjeta"}>
      <p className="practice-kind">
        {isQuiz ? "Pregunta del quiz" : "Flashcard"}
        {queued.state === null && " · nueva"}
      </p>
      <p className="practice-prompt">{item.prompt}</p>
      {!isQuiz &&
        (revealed ? (
          <>
            <p className="practice-answer">{item.answer}</p>
            <SourceLinks anchors={item.anchors} notesHref={notesHref} />
            <RatingButtons
              ratings={["again", "hard", "good", "easy"]}
              disabled={sending}
              onRate={(rating) => onReview({ rating })}
            />
          </>
        ) : (
          <button type="button" onClick={() => setPhase("revealed")}>
            Mostrar respuesta
          </button>
        ))}
      {isQuiz && (
        <form aria-label="Tu respuesta" onSubmit={check}>
          {item.question_type === "short_answer" ? (
            <label>
              Tu respuesta{" "}
              <input type="text" value={given} disabled={revealed} onChange={(e) => setGiven(e.target.value)} />
            </label>
          ) : (
            <ul className="practice-options">
              {item.options.map((option) => (
                <li key={option}>
                  <label>
                    <input
                      type="radio"
                      name={`opcion-${item.key}`}
                      value={option}
                      checked={given === option}
                      disabled={revealed}
                      onChange={() => setGiven(option)}
                    />{" "}
                    {option}
                  </label>
                </li>
              ))}
            </ul>
          )}
          {!revealed && <button type="submit">Comprobar</button>}
        </form>
      )}
      {isQuiz && revealed && (
        <div className="practice-feedback" role="group" aria-label="Corrección">
          {verdict === null ? (
            <p>
              La respuesta esperada es: <strong>{item.answer}</strong>. ¿La has acertado?{" "}
              <button type="button" onClick={() => setAssessed(true)}>
                Sí
              </button>{" "}
              <button type="button" onClick={() => setAssessed(false)}>
                No
              </button>
            </p>
          ) : verdict ? (
            <p className="practice-right">✓ Correcta</p>
          ) : (
            <p className="practice-wrong">
              ✗ Incorrecta. La respuesta es: <strong>{item.answer}</strong>
            </p>
          )}
          {item.explanation !== "" && <p>{item.explanation}</p>}
          <SourceLinks anchors={item.anchors} notesHref={notesHref} />
          {verdict === true && (
            <RatingButtons ratings={["hard", "good", "easy"]} disabled={sending} onRate={quizReview} />
          )}
          {verdict === false && (
            <button type="button" disabled={sending} onClick={() => quizReview()}>
              Siguiente
            </button>
          )}
        </div>
      )}
    </article>
  );
}

export default function PracticePage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [topicName, setTopicName] = useState(topicId);
  const [loaded, setLoaded] = useState<ActionResult<PracticeQueue> | null>(null);
  const [queue, setQueue] = useState<QueuedItem[]>([]);
  const [done, setDone] = useState(0);
  const [turn, setTurn] = useState(0);
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [last, setLast] = useState<string | null>(null);
  const reads = useRef(0);

  const load = useCallback(async () => {
    const read = ++reads.current;
    const result = await fetchPractice(subjectId, topicId);
    if (read !== reads.current) return;
    setLoaded(result);
    setQueue(result.kind === "ok" ? result.value.queue : []);
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
  const practice = loaded?.kind === "ok" ? loaded.value : null;
  const current = queue[0];

  async function review(answer: Omit<PracticeAnswer, "item">) {
    if (current === undefined) return;
    setSending(true);
    setFailure(null);
    const result = await sendReview(subjectId, topicId, { item: current.item.key, ...answer });
    setSending(false);
    if (result.kind !== "ok") {
      setFailure(describeActionFailure(result));
      return;
    }
    const outcome = result.value;
    const again = outcome.rating === "again";
    setLast(
      again
        ? "Volverá a salir al final de esta práctica."
        : `${RATING_LABELS[outcome.rating]}: volverá ${untilDue(outcome.state.due) ?? "más adelante"}.`,
    );
    setDone((value) => value + 1);
    setTurn((value) => value + 1);
    setQueue((previous) => {
      const [head, ...rest] = previous;
      return again && head !== undefined ? [...rest, { ...head, state: outcome.state }] : rest;
    });
  }

  return (
    <main className="practice-page">
      <p>
        <a href={base}>← Tema {topicName}</a>
      </p>
      <h1>Practicar {topicName}</h1>
      {loaded === null && <p>Cargando la práctica…</p>}
      {loaded !== null && loaded.kind !== "ok" && <p role="alert">{describeActionFailure(loaded)}</p>}
      {practice !== null && (
        <>
          <p className="practice-meta">
            {practice.counts.due} para repasar · {practice.counts.new} nuevas · {practice.counts.learned} de{" "}
            {practice.counts.total} ya vistas
          </p>
          {practice.warnings.map((warning) => (
            <p key={warning} className="practice-warning" role="note">
              {warning}
            </p>
          ))}
          {last !== null && (
            <p className="practice-last" role="status">
              {last}
            </p>
          )}
          {current !== undefined ? (
            <>
              <p className="practice-progress">
                {done === 1 ? "1 repasada" : `${done} repasadas`} · quedan {queue.length}
              </p>
              <ItemCard
                key={`${current.item.key}-${turn}`}
                queued={current}
                notesHref={`${base}/notes`}
                sending={sending}
                onReview={(answer) => void review(answer)}
              />
              {failure !== null && <p role="alert">{failure}</p>}
            </>
          ) : (
            practice.counts.total > 0 && (
              <section className="practice-done" aria-label="Fin de la práctica">
                <p>
                  {done > 0
                    ? `¡Hecho! Has repasado ${done === 1 ? "1 elemento" : `${done} elementos`}.`
                    : "No tienes nada que repasar ahora."}
                </p>
                {practice.next_due !== null && done === 0 && (
                  <p>Próximo repaso: {formatTime(practice.next_due)}.</p>
                )}
                <button type="button" onClick={() => void load()}>
                  Volver a comprobar
                </button>
              </section>
            )
          )}
        </>
      )}
      <p className="practice-links">
        <a href={`${base}/quiz`}>Ir al quiz completo</a>
      </p>
    </main>
  );
}
