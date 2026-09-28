import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { describeFailure, topicPath } from "../../desk/api";
import { describeTutorFailure, fetchTutorHistory, type SectionRef, type TutorTurn } from "../../tutor/api";
import { type OptionKey, STUDY_OPTIONS } from "../options";
import { askStudyChat, type GenerationResult, type StudyChatOutcome } from "./api";
import { useFollowLog } from "../../chat/useFollowLog";
import ReplyView from "./ReplyView";
import { useNotesImages } from "../../notes/useNotesImages";
import ChatComposer from "../../workspace/chat/ChatComposer";
import type { VoiceQuestionStarter } from "../../tutor/voiceQuestion";
import FeedbackChip from "../../chat/FeedbackChip";
import "../../chat/chat.css";
import "./studyChat.css";

/**
 * The question chat of the study screen (#336, epic #332): typed questions about the document,
 * answered by the read-only tutor in its written style (`POST .../tutor` with `style: "written"`,
 * #334). The answer streams, then its `[§anchor]` marks become section chips and its `[^label]`
 * marks source chips, inline where cited, handed to the page (`onOpenSection`, `onOpenSource`).
 * It never edits the notes: the only requests it sends are `GET` and `POST .../tutor`.
 *
 * A request such as «hazme un quiz» (#366, #367) makes the backend generate that material on the
 * same stream: the turn shows the `generation.started` line with a busy indicator, then the
 * generation's reply, its warnings and **Abrir «<opción>»**, which opens the option's panel
 * (`onOpenOption`), «Diapositivas» included (#382). The page gets the fresh study state (`onGenerated`).
 *
 * The log is its own scroll area and follows the newest turn as it streams (#412), unless the
 * student scrolled up (then «Nuevos mensajes ↓»); only the latest turn is announced, through a
 * polite live region, not the whole growing log.
 *
 * **Hablar** (#428) dictates one question: the interim text shows in the input, the final text
 * (trimmed to `MAX_QUESTION_CHARS`) is asked like a typed one; while an answer comes it stays in
 * the input, unsent.
 *
 * Since #487 it is the chat card of the shared frame (`WorkspaceFrame`), with the workspace chat's
 * input (`ChatComposer`): a textarea («Chat sobre el tema…»; Enter asks, Shift+Enter is a new line),
 * **Enviar** (the composer's own label, as in the Construir chat) and the microphone as an icon
 * button in one row below it; its heading is for screen readers only.
 */

export const MAX_QUESTION_CHARS = 1000;
export const READ_ONLY_LINE = "Solo respondo preguntas: no cambio los apuntes. Para cambiarlos, ve a";
export const BUSY_SENTENCE = "Espera a que termine la respuesta anterior.";
export const STALE_SECTION = "Esa sección ya no está en los apuntes";
export const THINKING = "Pensando…";
export const FOLLOW_BUTTON = "Nuevos mensajes ↓";

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
  /** A generation ended: the page refreshes its study state from `result.study`. */
  onGenerated?: (result: GenerationResult) => void;
  /** **Abrir «…»** of a generation turn: open that option's panel. */
  onOpenOption?: (key: OptionKey) => void;
  /** A phrase to put in the input (no send); a new `id` puts it again. */
  suggestion?: { text: string; id: number } | null;
  /** Listens for one spoken question; the Web Speech API by default. */
  listen?: VoiceQuestionStarter;
  /** Whether `listen` can work here; asked of the browser by default. */
  voiceSupported?: boolean;
}

/** A turn as the chat shows it: warnings as a list, the generation (if any) it made. */
type ChatTurn = TutorTurn & { warnings: string[] };

/** An answer as the live region reads it: without its `[§anchor]` and `[^label]` marks. */
const spoken = (text: string): string => text.replace(/\[(?:§|\^)[^\]]*\]/g, "").replace(/\s+([.,;:!?])/g, "$1").replace(/\s{2,}/g, " ").trim();

const chatTurn = (turn: TutorTurn): ChatTurn => ({ ...turn, warnings: turn.warning === null ? [] : [turn.warning] });

/** The option a generation turn opens; none for an option the page does not know. */
function openableOption(option: string) {
  return STUDY_OPTIONS.find((info) => info.key === option) ?? null;
}

type History = { state: "loading" } | { state: "ready" } | { state: "failed"; message: string };

interface Asking {
  question: string;
  reply: string;
  /** The `generation.started` line, once the backend said it is generating a material. */
  generating: string | null;
}

type Failure =
  | { kind: "message"; message: string; overCapQuestion: string | null }
  | { kind: "busy" }
  | { kind: "no-notes" };

function failureOf(outcome: Exclude<StudyChatOutcome, { kind: "ok" }>, question: string, hasNotes: boolean | null): Failure {
  if (outcome.kind === "refused") {
    if (outcome.overCap) return { kind: "message", message: outcome.detail, overCapQuestion: question };
    if (outcome.status === 409) {
      const noNotes = hasNotes === false || /no hay apuntes/i.test(outcome.detail);
      return noNotes ? { kind: "no-notes" } : { kind: "busy" };
    }
  }
  return { kind: "message", message: describeTutorFailure(outcome), overCapQuestion: null };
}

export default function StudyChat({
  subjectId,
  topicId,
  sections,
  hasNotes,
  onOpenSection,
  onOpenSource,
  onGenerated,
  onOpenOption,
  suggestion = null,
  listen,
  voiceSupported,
}: StudyChatProps) {
  const [history, setHistory] = useState<History>({ state: "loading" });
  const resolveImage = useNotesImages(subjectId, topicId);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [asking, setAsking] = useState<Asking | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const mounted = useRef(true);
  // What the live region says once an answer ended: never the history, only a turn asked here.
  const [answered, setAnswered] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  // Changes whenever a turn is added or the answer on its way grows.
  const content = `${turns.length}:${asking === null ? "" : `${asking.question}:${asking.generating ?? ""}:${asking.reply.length}`}`;
  const log = useFollowLog<HTMLOListElement>(content);

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
        setTurns(result.value.filter((turn) => turn.style === "written" || turn.generation !== null).map(chatTurn));
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
      setAnswered("");
      log.follow();
      setAsking({ question: text, reply: "", generating: null });
      const outcome = await askStudyChat(subjectId, topicId, text, {
        confirmOverCap,
        onGenerationStarted: (started) => {
          if (mounted.current) setAsking((now) => (now === null ? now : { ...now, reply: "", generating: started.text }));
        },
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
        const time = new Date().toISOString();
        const reply = outcome.value;
        if (reply.kind === "answer") {
          setTurns((now) => [...now, chatTurn({ ...reply.answer, time, generation: null })]);
          setAnswered(spoken(reply.answer.reply));
        } else {
          const { generation } = reply;
          setTurns((now) => [
            ...now,
            {
              time,
              style: "written",
              question: text,
              reply: generation.reply,
              refs: [],
              sections: [],
              warning: null,
              feedback: null,
              warnings: generation.warnings,
              generation: { option: generation.option, items: generation.items },
            },
          ]);
          setAnswered(generation.reply);
          onGenerated?.(generation);
        }
        // A question dictated while this answer came stays in the input.
        setDraft((now) => (now.trim() === text ? "" : now));
      } else {
        setDraft(text);
        setFailure(failureOf(outcome, text, hasNotes));
      }
      // The input was disabled while the answer came: give it the focus back.
      requestAnimationFrame(() => input.current?.focus({ preventScroll: true }));
    },
    [asking, hasNotes, onGenerated, subjectId, topicId, log.follow],
  );

  // A hint of an option («Pídelo en el chat: …») puts its phrase in the input, not sent.
  useEffect(() => {
    if (suggestion === null) return;
    setDraft(suggestion.text);
    input.current?.focus({ preventScroll: true });
  }, [suggestion]);

  const chips = (cited: SectionRef[]) => ({
    resolveImage,
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
  // Announced once per state, never on every streamed fragment.
  const announced = asking !== null ? `Asistente: ${asking.generating ?? THINKING}` : answered === "" ? "" : `Asistente: ${answered}`;
  const workspace = `${topicPath(subjectId, topicId)}/workspace`;

  return (
    <section className="ws-chat study-chat" aria-labelledby="study-chat-heading">
      <h2 id="study-chat-heading" className="study-chat-heading">
        Preguntas sobre el documento
      </h2>
      {history.state === "loading" && <p role="status">Cargando las preguntas anteriores…</p>}
      {history.state === "failed" && <p role="alert">{history.message}</p>}
      {history.state === "ready" && turns.length === 0 && !busy && (
        <p className="ws-chat-hint study-chat-empty">Pregunta lo que no entiendas: contesto con tus apuntes y cito dónde está.</p>
      )}
      <div className="ws-chat-scroll study-chat-scroll">
        <ol
          ref={log.ref}
          onScroll={log.onScroll}
          className="ws-chat-log chat-log study-chat-log"
          role="log"
          aria-live="off"
          aria-label="Preguntas y respuestas"
          tabIndex={0}
        >
          {turns.map((turn, index) => (
            <li className="chat-entry" key={`${turn.time}-${index}`}>
              <p className="chat-message">
                <span className="chat-who">Tú:</span> {turn.question}
              </p>
              <div className="chat-reply">
                <span className="chat-who">Asistente:</span>
                {turn.generation === null ? (
                  <ReplyView text={turn.reply} {...chips(turn.sections)} />
                ) : (
                  <p>{turn.reply}</p>
                )}
              </div>
              {turn.warnings.map((warning, w) => (
                <p className="chat-warning" key={w}>
                  {warning}
                </p>
              ))}
              {turn.feedback !== null && <FeedbackChip feedback={turn.feedback} />}
              {turn.generation !== null && <OpenButton option={turn.generation.option} onOpen={onOpenOption} />}
            </li>
          ))}
          {asking !== null && (
            <li className="chat-entry" aria-busy="true">
              <p className="chat-message">
                <span className="chat-who">Tú:</span> {asking.question}
              </p>
              <div className="chat-reply">
                <span className="chat-who">Asistente:</span>
                {asking.generating !== null ? (
                  <p className="study-chat-progress">
                    <span className="study-chat-spinner" aria-hidden="true" />
                    {asking.generating}
                  </p>
                ) : asking.reply === "" ? (
                  <p>{THINKING}</p>
                ) : (
                  <ReplyView text={asking.reply} {...chips([])} />
                )}
              </div>
            </li>
          )}
        </ol>
        {log.unseen && (
          <button type="button" className="study-chat-follow" onClick={log.follow}>
            {FOLLOW_BUTTON}
          </button>
        )}
      </div>
      <div className="study-chat-sr" aria-live="polite" data-testid="study-chat-latest">
        {announced}
      </div>
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
      <ChatComposer
        id="study-chat-question"
        label="Tu pregunta"
        placeholder="Chat sobre el tema…"
        value={draft}
        onChange={setDraft}
        onSubmit={() => void ask(draft)}
        canSubmit={!busy && draft.trim() !== ""}
        maxLength={MAX_QUESTION_CHARS}
        disabled={busy}
        inputRef={input}
        voice={{
          onFinal: (spokenQuestion) => {
            const question = spokenQuestion.slice(0, MAX_QUESTION_CHARS);
            setDraft(question);
            void ask(question);
          },
          disabled: busy,
          listen,
          voiceSupported,
        }}
      />
    </section>
  );
}

/** **Abrir «Quiz»** of a generation turn; nothing for an option the page does not know. */
function OpenButton({ option, onOpen }: { option: string; onOpen?: (key: OptionKey) => void }) {
  const info = openableOption(option);
  if (info === null) return null;
  return (
    <p className="study-chat-open">
      <button type="button" onClick={() => onOpen?.(info.key)}>
        Abrir «{info.title}»
      </button>
    </p>
  );
}
