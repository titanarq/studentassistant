import { MathText } from "../../math/Math";
import { type ReactNode, useId, useRef, useState } from "react";
import ChatMarkdown from "../../chat/ChatMarkdown";
import DiffView from "../../chat/DiffView";
import FeedbackChip from "../../chat/FeedbackChip";
import type { OpenSource } from "../../chat/EditorChat";
import { useFollowLog } from "../../chat/useFollowLog";
import { sourceItem } from "../resources";
import type { SourceSelection } from "../resources/selection";
import { reasonText } from "../resources/state";
import type { DoubtView, SpokenSpan, TriageTarget } from "./api";
import { capitalized, croppedImageName, isDiagram, sourceName } from "./sources";
import { canRetry, type ChatEntry } from "./turns";
import type { WorkspaceChat } from "./useWorkspaceChat";
import ChatComposer from "./ChatComposer";
import { doubtsLine as marksLine } from "../doubtMarks";
import "./chat.css";

/** `00:02:34`, from milliseconds since the start of the session. */
export function clock(ms: number): string {
  const total = Math.floor(ms / 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}`;
}

const HOUR = new Intl.DateTimeFormat("es-ES", { hour: "2-digit", minute: "2-digit" });

function hourOf(time: string | null): string | null {
  if (time === null) return null;
  const date = new Date(time);
  return Number.isNaN(date.getTime()) ? null : HOUR.format(date);
}

function MicIcon() {
  return (
    <svg className="ws-chat-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <path d="M12 18v3" />
    </svg>
  );
}

function UndoIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 14 4 9l5-5" />
      <path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />
    </svg>
  );
}

/** What the student said for a spoken request: the short summary, with … for the raw stretch. */
function SpokenRequest({ entry }: { entry: ChatEntry }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const hint = useId();
  const span: SpokenSpan | null = entry.transcript;
  const raw = span?.text ?? entry.message;
  const hour = hourOf(entry.time);
  return (
    <div className="ws-chat-request">
      <p className="ws-chat-who">
        <MicIcon />
        {hour !== null ? `Por voz · ${hour}` : "Por voz"}
      </p>
      <p className="ws-chat-said">
        Pediste: {entry.requestSummary ?? raw ?? "algo al asistente"}
        {raw !== null && raw !== "" && (
          <>
            {" "}
            <button
              type="button"
              className="ws-chat-more"
              aria-expanded={open}
              aria-controls={id}
              aria-label={open ? "Ocultar lo que dijiste" : "Ver lo que dijiste"}
              aria-describedby={hint}
              title={open ? "Ocultar lo que dijiste" : "Ver lo que dijiste, palabra por palabra"}
              onClick={() => setOpen((value) => !value)}
            >
              …
            </button>
          </>
        )}
      </p>
      {raw !== null && raw !== "" && (
        <span id={hint} className="ws-chat-sr">
          Muestra la transcripción de lo que dijiste y cuándo lo dijiste
        </span>
      )}
      {open && raw !== null && (
        <div id={id} className="ws-chat-transcript">
          <p className="ws-chat-quote">«{raw}»</p>
          {span !== null && span.endMs > 0 && (
            <p className="ws-chat-range">
              {clock(span.startMs)}–{clock(span.endMs)}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function TypedRequest({ entry, continued }: { entry: ChatEntry; continued: boolean }) {
  // A second request of the same typed message: the message is already shown above.
  if (continued) {
    return (
      <div className="ws-chat-request">
        <p className="ws-chat-who">Y además</p>
        <p className="ws-chat-said">{entry.requestSummary ?? entry.message ?? "Otra petición"}</p>
      </div>
    );
  }
  return (
    <div className="ws-chat-request">
      <p className="ws-chat-who">{entry.kind === "explain" ? "Preguntaste" : "Escribiste"}</p>
      <p className="ws-chat-said">{entry.message ?? "Un mensaje escrito"}</p>
    </div>
  );
}

/**
 * How the request of a turn shows, one case per origin: said, typed, or none for what the
 * assistant does on its own (a doubt, a batch of a run); a run started elsewhere is headed as such.
 */
function Request({ entry, continued }: { entry: ChatEntry; continued: boolean }) {
  if (entry.parent !== null) return null;
  switch (entry.origin) {
    case "voice":
      return <SpokenRequest entry={entry} />;
    case "typed":
      return <TypedRequest entry={entry} continued={continued} />;
    case "system":
      return entry.kind === "prepare_notes" ? <p className="ws-chat-who">Preparación del tema</p> : null;
  }
}

const RUNNING: Record<string, string> = {
  prepare_notes: "Preparando los apuntes del tema…",
  incorporate: "Incorporando…",
  triage: "Un momento…",
  doubt_answer: "Aplicando tu respuesta…",
  study: "Pasando a Estudiar…",
};

/** What the reply slot says until the reply arrives (#452: «Respondiendo…» from the moment it is sent). */
export const ANSWERING = "Respondiendo…";

function replyPlaceholder(entry: ChatEntry): string | null {
  if (entry.status === "sending") return ANSWERING;
  if (entry.status === "queued") return "En cola…";
  if (entry.status === "running") return RUNNING[entry.kind] ?? ANSWERING;
  return null;
}

/** A small spinner beside the reply while the turn is on its way (decorative: the text says it). */
function Spinner() {
  return <span className="ws-chat-spinner" aria-hidden="true" data-testid="ws-chat-spinner" />;
}

const onItsWay = (entry: ChatEntry): boolean => entry.status === "sending" || entry.status === "queued" || entry.status === "running";

/** A source named in the chat, opening it in Recursos when it can. */
function SourceButton({
  sourceId,
  onOpenSource,
  text,
  spoken,
}: {
  sourceId: string;
  onOpenSource?: OpenSource;
  text?: string;
  /** The source's name in the button's label, when `sourceName` would not name it right. */
  spoken?: string;
}) {
  const name = text ?? sourceName(sourceId);
  const item = sourceItem(sourceId);
  if (onOpenSource === undefined || item === null) return <>{name}</>;
  return (
    <button
      type="button"
      className="ws-chat-ref"
      aria-label={`Ver la fuente: ${spoken ?? sourceName(sourceId)}`}
      onClick={(event) => onOpenSource(item.label, event.currentTarget, item.definition)}
    >
      {name}
    </button>
  );
}

function Sources({ sourceIds, onOpenSource }: { sourceIds: string[]; onOpenSource?: OpenSource }) {
  return (
    <>
      {sourceIds.map((id, index) => (
        <span key={id}>
          {index > 0 && ", "}
          <SourceButton sourceId={id} onOpenSource={onOpenSource} />
        </span>
      ))}
    </>
  );
}

/** «en blanco», «repetida de la página 2»: the Recursos tab's reason text, mid-sentence. */
const lowered = (text: string): string => text.charAt(0).toLowerCase() + text.slice(1);

/**
 * A set-aside with triage reasons (#351), one line per target: «Página 9 apartada: en blanco»,
 * «Página 1 ya estaba apartada: repetida de la página 2», «Página 4 apartada».
 */
function TriageLines({ targets, onOpenSource }: { targets: TriageTarget[]; onOpenSource?: OpenSource }) {
  return (
    <div className="ws-chat-triage">
      {targets.map((target) => {
        const reasons = target.reasons.map((reason) => lowered(reasonText(reason, target.duplicateOf)));
        return (
          <p key={target.sourceId} className="ws-chat-line">
            <SourceButton sourceId={target.sourceId} onOpenSource={onOpenSource} text={capitalized(sourceName(target.sourceId))} />
            {target.already ? " ya estaba apartada" : " apartada"}
            {reasons.length > 0 ? `: ${reasons.join(", ")}` : ""}
          </p>
        );
      })}
    </div>
  );
}

function doubtsLine(count: number): string {
  return count === 1
    ? "Ha surgido 1 duda: la tienes marcada en los apuntes."
    : `Han surgido ${count} dudas: las tienes marcadas en los apuntes.`;
}

/** The student's answer to a doubt by a button (#516): a suggestion (1-based) or the right source. */
export interface DoubtChoice {
  suggestion?: number;
  source_id?: string;
}

/** Sends a doubt's answer (`POST .../doubts/{id}/answer`); resolves to a Spanish refusal, or `null`. */
export type AnswerDoubt = (pendingId: string, choice: DoubtChoice) => Promise<string | null>;

/** The doubts marked in the notes (#516), for the line above the input. */
export interface MarksLine {
  count: number;
  onNext: () => void;
  busy: boolean;
  problem: string | null;
}

const DOUBT_BADGE: Record<string, string> = {
  open: "Duda",
  resolved: "Duda resuelta",
  auto_resolved: "Duda resuelta",
  dismissed: "Duda descartada",
};

/**
 * A doubt asked in the chat (#325, #516): highlighted, with what it is about, the question, the
 * suggestions as numbered buttons and, for a contradiction, what each source says as a button
 * (the source itself opens in Recursos). The student answers by pressing one (`onAnswer`, the
 * doubts route), or by typing or saying it (a `doubt_answer` turn); `doubt.resolved` marks it
 * answered.
 */
function DoubtEntry({
  doubt,
  onOpenSource,
  onAnswer,
  capturing,
}: {
  doubt: DoubtView;
  onOpenSource?: OpenSource;
  onAnswer?: AnswerDoubt;
  capturing: boolean;
}) {
  const [chosen, setChosen] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const open = doubt.status === "open";
  const withOptions = new Set(doubt.options.map((option) => option.sourceId));
  const others = doubt.refs.filter((ref) => !withOptions.has(ref));
  const canChoose = open && onAnswer !== undefined && !sending;
  const choose = async (label: string, choice: DoubtChoice) => {
    if (!canChoose) return;
    setSending(true);
    setProblem(null);
    setChosen(label);
    const refusal = await onAnswer(doubt.pendingId, choice);
    setSending(false);
    if (refusal !== null) {
      setChosen(null);
      setProblem(refusal);
    }
  };
  const answer = doubt.answer ?? (open ? null : chosen);
  return (
    <div className={open ? "ws-chat-doubt" : "ws-chat-doubt ws-chat-doubt-closed"}>
      <p className="ws-chat-who">
        Asistente <span className="ws-chat-badge">{DOUBT_BADGE[doubt.status] ?? "Duda"}</span>
      </p>
      {doubt.text !== "" && doubt.text !== doubt.question && <p className="ws-chat-explanation"><MathText text={doubt.text} /></p>}
      <p className="ws-chat-text"><MathText text={doubt.question} /></p>
      {doubt.suggestions.length > 0 && (
        <ol className="ws-chat-suggestions" aria-label="Sugerencias">
          {doubt.suggestions.map((suggestion, index) => (
            <li key={index}>
              <button
                type="button"
                className="ws-chat-choice"
                disabled={!canChoose}
                aria-pressed={chosen === suggestion ? "true" : undefined}
                onClick={() => void choose(suggestion, { suggestion: index + 1 })}
              >
                <MathText text={suggestion} />
              </button>
            </li>
          ))}
        </ol>
      )}
      {doubt.options.length > 0 && (
        <ul className="ws-chat-options" aria-label="Qué dice cada fuente">
          {doubt.options.map((option) => (
            <li key={option.sourceId}>
              <button
                type="button"
                className="ws-chat-choice"
                disabled={!canChoose}
                aria-label={`Es correcto: «${option.says}» (${sourceName(option.sourceId)})`}
                aria-pressed={chosen === option.says ? "true" : undefined}
                onClick={() => void choose(option.says, { source_id: option.sourceId })}
              >
                «{option.says}»
              </button>{" "}
              <SourceButton sourceId={option.sourceId} onOpenSource={onOpenSource} text={capitalized(sourceName(option.sourceId))} />
            </li>
          ))}
        </ul>
      )}
      {others.length > 0 && (
        <p className="ws-chat-refs">
          Sobre: <Sources sourceIds={others} onOpenSource={onOpenSource} />
        </p>
      )}
      {open && sending && <p className="ws-chat-hint">Aplicando tu respuesta…</p>}
      {open && !sending && (
        <p className="ws-chat-hint">
          {onAnswer !== undefined && (doubt.suggestions.length > 0 || doubt.options.length > 0) ? "Pulsa una respuesta o contesta" : "Contesta"}{" "}
          {capturing ? "escribiendo o de viva voz" : "escribiendo o con el micrófono"}: «la 2», «pone “escrita”»…
        </p>
      )}
      {problem !== null && (
        <p className="ws-chat-warning" role="alert">
          No se pudo aplicar tu respuesta: {problem}
        </p>
      )}
      {!open && answer !== null && <p className="ws-chat-answer">Respondiste: «{answer}»</p>}
      {doubt.status === "dismissed" && <p className="ws-chat-resolved">Descartada.</p>}
      {doubt.status !== "open" && doubt.status !== "dismissed" && (
        <p className="ws-chat-resolved">
          {doubt.status === "auto_resolved" ? "Resuelta con las fuentes" : "Respondida"}
          {doubt.resolution !== null ? `: ${doubt.resolution}` : "."}
        </p>
      )}
    </div>
  );
}

interface ReplyProps {
  entry: ChatEntry;
  versionsPath: string;
  onOpenSource?: OpenSource;
  onRetry: (key: string) => void;
  idle: boolean;
  /** A capture is running: the student can also answer by voice. */
  capturing: boolean;
  /** Answers a doubt by a button (#516). */
  onAnswer?: AnswerDoubt;
  /** A whole-topic run: its batches, shown below its progress. */
  batches?: ReactNode;
}

function Reply({ entry, versionsPath, onOpenSource, onRetry, idle, capturing, onAnswer, batches }: ReplyProps) {
  const [showDiff, setShowDiff] = useState(false);
  const diffId = useId();
  const placeholder = replyPlaceholder(entry);
  const text = entry.reply !== "" ? entry.reply : placeholder;
  if (entry.kind === "doubt" && entry.doubt !== null)
    return <DoubtEntry doubt={entry.doubt} onOpenSource={onOpenSource} onAnswer={onAnswer} capturing={capturing} />;
  if (entry.kind === "doubts_resolved") return <p className="ws-chat-line"><MathText text={entry.reply} /></p>;
  if (entry.kind === "triage" && entry.status === "done" && entry.decision === "set_aside" && entry.targets.some((t) => t.reasons.length > 0)) {
    return <TriageLines targets={entry.targets} onOpenSource={onOpenSource} />;
  }
  if (entry.kind === "triage" && entry.status !== "failed" && text !== null) {
    // One short line of what was set aside or restored (a set-aside with no triage reason, a
    // restore, or a turn recorded before the reasons were).
    return <p className={entry.reply === "" ? "ws-chat-line ws-chat-waiting" : "ws-chat-line"}>{text}</p>;
  }
  const batch = entry.parent !== null;
  return (
    <div className="ws-chat-reply">
      {entry.kind === "incorporate" && entry.sourceIds.length > 0 && (
        <p className="ws-chat-incorporated">
          {capitalized(entry.status === "done" ? "incorporadas" : "incorporando")}: <Sources sourceIds={entry.sourceIds} onOpenSource={onOpenSource} />
        </p>
      )}
      {entry.progress !== null && (
        <p className="ws-chat-progress">
          <progress max={entry.progress.total} value={entry.progress.done} aria-hidden="true" />{" "}
          {entry.progress.done} de {entry.progress.total} páginas
        </p>
      )}
      {batches}
      {(text !== null || entry.status !== "failed") && !(batch && text === null) && (
        <>
          {!batch && <p className="ws-chat-who">Asistente</p>}
          {entry.reply !== "" ? (
            <div className="ws-chat-text">
              <ChatMarkdown text={entry.reply} />
            </div>
          ) : (
            <p className="ws-chat-text ws-chat-waiting">
              {onItsWay(entry) && <Spinner />}
              {text}
            </p>
          )}
        </>
      )}
      {entry.refs.length > 0 && (
        <p className="ws-chat-refs">
          Fuentes:{" "}
          {entry.refs.map((ref, index) => (
            <span key={`${ref.label}-${index}`}>
              {index > 0 && ", "}
              {onOpenSource !== undefined ? (
                <button
                  type="button"
                  className="ws-chat-ref"
                  aria-label={`Ver la fuente: ${ref.text}`}
                  onClick={(event) => onOpenSource(ref.label, event.currentTarget)}
                >
                  {ref.text}
                </button>
              ) : (
                ref.text
              )}
            </span>
          ))}
        </p>
      )}
      {entry.status === "done" && entry.applied && entry.summary !== null && (
        <p className="ws-chat-applied">
          {entry.undone ? "Cambio deshecho" : "Cambio aplicado"}: {entry.summary}
          {!entry.undone && entry.diff !== null && (
            <>
              {" · "}
              <button
                type="button"
                className="ws-chat-link"
                aria-expanded={showDiff}
                aria-controls={diffId}
                onClick={() => setShowDiff((value) => !value)}
              >
                {showDiff ? "Ocultar los cambios" : "Ver los cambios"}
              </button>
            </>
          )}
          {!entry.undone && entry.diff === null && entry.commit !== null && (
            <>
              {" · "}
              <a className="ws-chat-link" href={versionsPath}>
                Ver las versiones
              </a>
            </>
          )}
        </p>
      )}
      {showDiff && entry.diff !== null && !entry.undone && (
        <div id={diffId}>
          <DiffView diff={entry.diff} />
        </div>
      )}
      {entry.status === "done" && entry.action?.kind === "go_study" && (
        <p className="ws-chat-actions">
          <a className="ws-chat-go-study" href={entry.action.path}>
            Ir a Estudiar
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M5 12h14" />
              <path d="M13 6l6 6-6 6" />
            </svg>
          </a>
        </p>
      )}
      {entry.status === "done" && entry.feedback !== null && <FeedbackChip feedback={entry.feedback} />}
      {entry.status === "done" && entry.cropId !== null && !entry.undone && (
        <p className="ws-chat-incorporated">
          {isDiagram(entry.cropId) ? "Diagrama añadido:" : "Recorte añadido:"}{" "}
          <SourceButton
            sourceId={entry.cropId}
            onOpenSource={onOpenSource}
            text={capitalized(croppedImageName(entry.cropId))}
            spoken={croppedImageName(entry.cropId)}
          />
        </p>
      )}
      {entry.kind === "incorporate" && entry.status === "done" && entry.doubts > 0 && (
        <p className="ws-chat-doubts">{doubtsLine(entry.doubts)}</p>
      )}
      {entry.warning !== null && <p className="ws-chat-warning">{entry.warning}</p>}
      {entry.failure !== null && (
        <div className="ws-chat-failure">
          <p role="alert" className="ws-chat-warning">
            No se pudo completar: {entry.failure}
          </p>
          {canRetry(entry) && (
            <button type="button" disabled={!idle} onClick={() => onRetry(entry.key)}>
              Continuar igualmente
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The workspace's chat panel (#317, #329, epic #311), the one place the student drives the work:
 * each request as a short line -- a spoken one as "Pediste: <summary>" with **…** to show the raw
 * transcript of that stretch and its time range --, then the assistant's reply as it streams, the
 * applied change with its diff, and failures in Spanish (a reached cost cap offers "Continuar
 * igualmente"). Requests waiting for their turn say "En cola…". Incorporations say what they
 * incorporated and how many doubts they raised; a whole-topic run shows its progress and its
 * batches; setting pages aside or restoring them is one short line; a doubt asked in the chat is
 * highlighted, and answered by pressing a suggestion or a source, typing or saying it; the doubts
 * marked in the notes are one line above the input, «Tienes N dudas marcadas en los apuntes», with
 * «Siguiente duda.» (#516; they are never asked one after another). "Ya está, quiero estudiar" (#335, #337) is
 * answered with one line and a single **Ir a Estudiar** button to the study screen. Below, a textarea (Enter sends, Shift+Enter
 * is a new line) with, in one row, **Enviar** and the icon buttons for the microphone and "Deshacer
 * el último cambio" (#458; `ChatComposer`, shared with the study chat since #487); no heading above
 * the log (#458), the input's placeholder says what it is.
 *
 * The log is its own scroll area and follows the newest turn (#412) unless the student scrolled up
 * (then «Nuevos mensajes ↓» brings them back); only the latest turn is announced, through a polite
 * live region, not the whole growing log. While a capture runs (`capturing`) what the student says
 * reaches the chat through the capture; otherwise the microphone button (#428) dictates one message into the
 * input and sends it like a typed one (kept in the input, unsent, while the assistant is busy).
 * Starting a capture removes the button and stops its recognition. The sources selected in
 * **Recursos** (#432) show as chips above the input and go with each message sent.
 */
/** What the live region says of the latest turn: its status while it runs, its reply once done. */
export function latestLine(entry: ChatEntry | undefined): string {
  if (entry === undefined) return "";
  if (entry.kind === "doubt" && entry.doubt !== null) {
    return entry.doubt.status === "open" ? `Asistente, duda: ${entry.doubt.question}` : "";
  }
  const waiting = replyPlaceholder(entry);
  if (waiting !== null) return `Asistente: ${waiting}`;
  if (entry.status === "failed") return "";
  return entry.reply !== "" ? `Asistente: ${entry.reply}` : "";
}

export const FOLLOW_BUTTON = "Nuevos mensajes ↓";

/** More chips than this show the first `CHIPS_SHOWN` and «+N». */
export const MAX_CHIPS = 6;
const CHIPS_SHOWN = 5;

/**
 * The Recursos selection above the chat input (#432): one chip per selected source with × to
 * deselect it (the first five and «+N» past six) and «Quitar selección». The next message carries
 * them; the selection stays after it is sent, for a follow-up on the same pages.
 */
function SelectionChips({ selection }: { selection: SourceSelection }) {
  const { selected } = selection;
  if (selected.length === 0) return null;
  const shown = selected.length > MAX_CHIPS ? selected.slice(0, CHIPS_SHOWN) : selected;
  const rest = selected.slice(shown.length);
  return (
    <div className="ws-chat-chips" role="group" aria-label="Fuentes seleccionadas para el mensaje">
      <ul>
        {shown.map((source) => (
          <li key={source.id} className="ws-chat-chip">
            <span>{source.title}</span>
            <button type="button" aria-label={`Quitar ${source.title} de la selección`} onClick={() => selection.deselect(source.id)}>
              ×
            </button>
          </li>
        ))}
        {rest.length > 0 && (
          <li className="ws-chat-chip ws-chat-chip-more" title={rest.map((source) => source.title).join(", ")}>
            {`+${rest.length}`}
          </li>
        )}
      </ul>
      <button type="button" className="ws-chat-link" onClick={selection.clear}>
        Quitar selección
      </button>
    </div>
  );
}

export default function ChatPanel({
  chat,
  versionsPath,
  onOpenSource,
  capturing = false,
  selection = null,
  onAnswerDoubt,
  marks = null,
}: {
  chat: WorkspaceChat;
  /** The topic's versions page, where a change read from the history is compared. */
  versionsPath: string;
  onOpenSource?: OpenSource;
  /** A capture of this topic is running: what the student says reaches the chat too. */
  capturing?: boolean;
  /** The Recursos selection (#432): chips above the input, sent with each message. */
  selection?: SourceSelection | null;
  /** Answers a doubt asked in the chat by a button (#516). */
  onAnswerDoubt?: AnswerDoubt;
  /** The doubts marked in the notes (#516): «Tienes N dudas marcadas en los apuntes». */
  marks?: MarksLine | null;
}) {
  const [draft, setDraft] = useState("");
  const idle = chat.busy === null;
  const keys = new Set(chat.entries.map((entry) => entry.key));
  const top = chat.entries.filter((entry) => entry.parent === null || !keys.has(entry.parent));
  const children = new Map<string, ChatEntry[]>();
  for (const entry of chat.entries) {
    if (entry.parent !== null && keys.has(entry.parent)) children.set(entry.parent, [...(children.get(entry.parent) ?? []), entry]);
  }
  // Changes whenever a turn is added, changes state or its streamed reply grows.
  const content = chat.entries.map((entry) => `${entry.key}:${entry.status}:${entry.reply.length}:${entry.doubt?.status ?? ""}`).join("|");
  const log = useFollowLog<HTMLOListElement>(content);
  // Turns seen on their way (sent, queued, running) on this page: the history is never announced.
  const live = useRef(new Set<string>());
  for (const entry of chat.entries) if (entry.status !== "done" && entry.status !== "failed") live.current.add(entry.key);
  const latest = top.length > 0 ? top[top.length - 1] : undefined;
  const latestChildren = latest !== undefined ? (children.get(latest.key) ?? []) : [];
  const current = latestChildren.length > 0 && latest?.status === "running" ? latestChildren[latestChildren.length - 1] : latest;
  const announced = current !== undefined && (live.current.has(current.key) || current.kind === "doubt") ? latestLine(current) : "";

  const send = (text: string) => {
    if (!idle || text.trim() === "") return;
    // The input is emptied only once the message is in the history (it shows there at once).
    if (!chat.send(text, selection?.selected.map((source) => source.id) ?? [])) return;
    setDraft("");
    log.follow();
  };
  const dictated = (text: string) => {
    setDraft(text);
    send(text);
  };

  const placeholder = chat.doubtAsked
    ? capturing
      ? "Responde a la duda (escribiendo o de viva voz) o pide otra cosa…"
      : "Responde a la duda (escribiendo o con el micrófono) o pide otra cosa…"
    : "Chatea con el asistente…";

  return (
    <section className="ws-chat" aria-label="Chat con el asistente">
      {chat.connection === "down" && (
        <p className="ws-chat-status" role="status">
          Sin conexión en directo con el asistente; reintentando…
        </p>
      )}
      {chat.historyFailure !== null && <p role="alert">{chat.historyFailure}</p>}
      {chat.entries.length === 0 && chat.historyFailure === null && (
        <p className="ws-chat-hint">
          {capturing ? "Pídele cambios al asistente hablando o escribiendo" : "Pídele cambios al asistente escribiendo o con el micrófono"}: «pon esto como
          definición», «haz una tabla con las tres causas»…
        </p>
      )}
      <div className="ws-chat-scroll">
        <ol
          ref={log.ref}
          onScroll={log.onScroll}
          className="ws-chat-log"
          role="log"
          aria-live="off"
          aria-label="Conversación con el asistente"
          tabIndex={0}
        >
          {top.map((entry, index) => {
            const own = children.get(entry.key) ?? [];
            const continued = entry.messageId !== null && top.slice(0, index).some((e) => e.messageId === entry.messageId);
            return (
              <li
                key={entry.key}
                className={entry.kind === "doubt" ? "ws-chat-entry ws-chat-entry-doubt" : "ws-chat-entry"}
                aria-busy={entry.status === "running" ? "true" : undefined}
              >
                <Request entry={entry} continued={continued} />
                <Reply
                  entry={entry}
                  versionsPath={versionsPath}
                  onOpenSource={onOpenSource}
                  onRetry={chat.retry}
                  idle={idle}
                  capturing={capturing}
                  onAnswer={onAnswerDoubt}
                  batches={
                    own.length > 0 && (
                      <ol className="ws-chat-batches" aria-label="Tandas de la preparación">
                        {own.map((child) => (
                          <li key={child.key} className="ws-chat-batch" aria-busy={child.status === "running" ? "true" : undefined}>
                            <Reply
                              entry={child}
                              versionsPath={versionsPath}
                              onOpenSource={onOpenSource}
                              onRetry={chat.retry}
                              idle={idle}
                              capturing={capturing}
                            />
                          </li>
                        ))}
                      </ol>
                    )
                  }
                />
              </li>
            );
          })}
        </ol>
        {log.unseen && (
          <button type="button" className="ws-chat-follow" onClick={log.follow}>
            {FOLLOW_BUTTON}
          </button>
        )}
      </div>
      <div className="ws-chat-sr" aria-live="polite" data-testid="ws-chat-latest">
        {announced}
      </div>
      <div aria-live="polite">
        {chat.busy === "undo" && <p>Deshaciendo el último cambio…</p>}
        {chat.notice !== null && <p>{chat.notice}</p>}
      </div>
      {marks !== null && marks.count > 0 && (
        <div className="ws-chat-marks" role="status">
          <span>{marksLine(marks.count)}</span>
          <button type="button" className="ws-chat-link ws-chat-marks-next" onClick={marks.onNext} disabled={marks.busy}>
            Siguiente duda.
          </button>
        </div>
      )}
      {marks !== null && marks.problem !== null && (
        <p className="ws-chat-warning" role="alert">
          {marks.problem}
        </p>
      )}
      <ChatComposer
        id="ws-chat-input"
        label="Mensaje para el asistente"
        placeholder={placeholder}
        value={draft}
        onChange={setDraft}
        onSubmit={() => send(draft)}
        canSubmit={idle && draft.trim() !== ""}
        maxLength={4000}
        voice={capturing ? null : { onFinal: dictated }}
        before={selection !== null && <SelectionChips selection={selection} />}
        actions={
          <button
            type="button"
            className="ws-chat-icon-button"
            aria-label="Deshacer el último cambio"
            title="Deshacer el último cambio"
            onClick={chat.undo}
            disabled={!idle || !chat.canUndo}
          >
            <UndoIcon />
          </button>
        }
      />
    </section>
  );
}
