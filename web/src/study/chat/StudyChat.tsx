import { type FormEvent, type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { describeFailure, topicPath } from "../../desk/api";
import {
  askTutor,
  describeTutorFailure,
  fetchTutorHistory,
  type SectionRef,
  type TutorOutcome,
  type TutorTurn,
} from "../../tutor/api";
import ReplyView from "./ReplyView";
import "../../chat/chat.css";
import "./studyChat.css";

/**
 * The question chat of the study screen (#336, epic #332): typed questions about the document,
 * answered by the read-only tutor in its written style (`POST .../tutor` with `style: "written"`,
 * #334). The answer streams, then its `[§anchor]` marks become section chips and its `[^label]`
 * marks source chips, inline where cited, handed to the page (`onOpenSection`, `onOpenSource`).
 * It never edits the notes: the only requests it sends are `GET` and `POST .../tutor`.
 */

export const MAX_QUESTION_CHARS = 1000;
export const READ_ONLY_LINE = "Solo respondo preguntas: no cambio los apuntes. Para cambiarlos, ve a";
export const BUSY_SENTENCE = "Espera a que termine la respuesta anterior.";
export const STALE_SECTION = "Esa sección ya no está en los apuntes";
export const THINKING = "Pensando…";

export interface StudyChatProps {
  subjectId: string;
  topicId: string;
  /** Anchor -> title of every section the document has now; a cited anchor it lacks is stale. */
  sections: ReadonlyMap<string, string>;
  /** `false` when the topic has no notes yet (the 409 of a question says so too). */
  hasNotes: boolean | null;
  /** A section chip: scroll the document to that section and highlight it. */
  onOpenSection: (anchor: string) => void;
  /** A source chip: highlight the blocks citing `label` and open its source; `trigger` gets the focus back. */
  onOpenSource: (label: string, trigger: HTMLElement) => void;
}

type History = { state: "loading" } | { state: "ready" } | { state: "failed"; message: string };

interface Asking {
  question: string;
  reply: string;
}

type Failure =
  | { kind: "message"; message: string; overCapQuestion: string | null }
  | { kind: "busy" }
  | { kind: "no-notes" };

function failureOf(outcome: Exclude<TutorOutcome, { kind: "ok" }>, question: string, hasNotes: boolean | null): Failure {
  if (outcome.kind === "refused") {
    if (outcome.overCap) return { kind: "message", message: outcome.detail, overCapQuestion: question };
    if (outcome.status === 409) {
      const noNotes = hasNotes === false || /no hay apuntes/i.test(outcome.detail);
      return noNotes ? { kind: "no-notes" } : { kind: "busy" };
    }
  }
  return { kind: "message", message: describeTutorFailure(outcome), overCapQuestion: null };
}

export default function StudyChat({ subjectId, topicId, sections, hasNotes, onOpenSection, onOpenSource }: StudyChatProps) {
  const [history, setHistory] = useState<History>({ state: "loading" });
  const [turns, setTurns] = useState<TutorTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [asking, setAsking] = useState<Asking | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const mounted = useRef(true);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void fetchTutorHistory(subjectId, topicId).then((result) => {
      if (cancelled) return;
      if (result.kind === "ok") {
        // The voice tutor's spoken turns are another conversation of the same file.
        setTurns(result.value.filter((turn) => turn.style === "written"));
        setHistory({ state: "ready" });
      } else if (result.kind === "not-found") {
        setHistory({ state: "ready" });
      } else {
        setHistory({ state: "failed", message: `No se han podido cargar las preguntas anteriores: ${describeFailure(result)}` });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  const ask = useCallback(
    async (question: string, confirmOverCap = false) => {
      const text = question.trim();
      if (text === "" || asking !== null) return;
      setFailure(null);
      setAsking({ question: text, reply: "" });
      const outcome = await askTutor(subjectId, topicId, text, {
        style: "written",
        confirmOverCap,
        onDelta: (delta) => {
          if (mounted.current) setAsking((now) => (now === null ? now : { ...now, reply: now.reply + delta }));
        },
        onRestart: () => {
          if (mounted.current) setAsking((now) => (now === null ? now : { ...now, reply: "" }));
        },
      });
      if (!mounted.current) return;
      setAsking(null);
      if (outcome.kind === "ok") {
        setTurns((now) => [...now, { ...outcome.value, time: new Date().toISOString() }]);
        setDraft("");
      } else {
        setDraft(text);
        setFailure(failureOf(outcome, text, hasNotes));
      }
      // The input was disabled while the answer came: give it the focus back.
      requestAnimationFrame(() => input.current?.focus({ preventScroll: true }));
    },
    [asking, hasNotes, subjectId, topicId],
  );

  function submit(event: FormEvent) {
    event.preventDefault();
    void ask(draft);
  }

  const chips = (cited: SectionRef[]) => ({
    renderSection: (anchor: string, key: string): ReactNode => {
      const title = cited.find((s) => s.anchor === anchor)?.title ?? sections.get(anchor) ?? anchor;
      const stale = !sections.has(anchor);
      return (
        <button
          key={key}
          type="button"
          className="study-chip"
          aria-label={`Ir a la sección ${title}`}
          title={stale ? STALE_SECTION : undefined}
          disabled={stale}
          onClick={() => onOpenSection(anchor)}
        >
          § {title}
        </button>
      );
    },
    renderSource: (label: string, key: string): ReactNode => (
      <button
        key={key}
        type="button"
        className="study-chip"
        aria-label={`Ver la fuente ${label}`}
        onClick={(event) => onOpenSource(label, event.currentTarget)}
      >
        {label}
      </button>
    ),
  });

  const busy = asking !== null;
  const workspace = `${topicPath(subjectId, topicId)}/workspace`;

  return (
    <section className="study-chat panel" aria-labelledby="study-chat-heading">
      <h2 id="study-chat-heading">Preguntas sobre el documento</h2>
      {history.state === "loading" && <p role="status">Cargando las preguntas anteriores…</p>}
      {history.state === "failed" && <p role="alert">{history.message}</p>}
      {history.state === "ready" && turns.length === 0 && !busy && (
        <p className="study-chat-empty">Pregunta lo que no entiendas: contesto con tus apuntes y cito dónde está.</p>
      )}
      <ol className="chat-log study-chat-log" role="log" aria-live="polite" aria-label="Preguntas y respuestas">
        {turns.map((turn, index) => (
          <li className="chat-entry" key={`${turn.time}-${index}`}>
            <p className="chat-message">
              <span className="chat-who">Tú:</span> {turn.question}
            </p>
            <div className="chat-reply">
              <span className="chat-who">Asistente:</span>
              <ReplyView text={turn.reply} {...chips(turn.sections)} />
            </div>
            {turn.warning !== null && <p className="chat-warning">{turn.warning}</p>}
          </li>
        ))}
        {asking !== null && (
          <li className="chat-entry" aria-busy="true">
            <p className="chat-message">
              <span className="chat-who">Tú:</span> {asking.question}
            </p>
            <div className="chat-reply">
              <span className="chat-who">Asistente:</span>
              {asking.reply === "" ? <p>{THINKING}</p> : <ReplyView text={asking.reply} {...chips([])} />}
            </div>
          </li>
        )}
      </ol>
      {failure !== null && (
        <div role="alert" className="study-chat-failure">
          {failure.kind === "busy" && <p>{BUSY_SENTENCE}</p>}
          {failure.kind === "no-notes" && (
            <p>
              Todavía no hay apuntes: constrúyelos en <a href={workspace}>Construir</a>.
            </p>
          )}
          {failure.kind === "message" && (
            <>
              <p>{failure.message}</p>
              {failure.overCapQuestion !== null && (
                <button type="button" disabled={busy} onClick={() => void ask(failure.overCapQuestion ?? "", true)}>
                  Continuar igualmente
                </button>
              )}
            </>
          )}
        </div>
      )}
      <p className="study-chat-note">
        {READ_ONLY_LINE} <a href={workspace}>Construir</a>.
      </p>
      <form className="study-chat-form" onSubmit={submit}>
        <label className="study-chat-label" htmlFor="study-chat-question">
          Tu pregunta
        </label>
        <input
          id="study-chat-question"
          ref={input}
          type="text"
          value={draft}
          maxLength={MAX_QUESTION_CHARS}
          placeholder="Pregunta sobre el documento…"
          onChange={(event) => setDraft(event.target.value)}
          disabled={busy}
        />
        <button type="submit" disabled={busy || draft.trim() === ""}>
          Preguntar
        </button>
      </form>
    </section>
  );
}
