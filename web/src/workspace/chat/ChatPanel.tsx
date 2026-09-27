import { type FormEvent, type KeyboardEvent, type ReactNode, useId, useState } from "react";
import DiffView from "../../chat/DiffView";
import type { OpenSource } from "../../chat/EditorChat";
import { sourceItem } from "../resources";
import { reasonText } from "../resources/state";
import type { DoubtView, SpokenSpan, TriageTarget } from "./api";
import { capitalized, sourceName } from "./sources";
import { canRetry, type ChatEntry } from "./turns";
import type { WorkspaceChat } from "./useWorkspaceChat";
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

/** What the student said for a spoken request: the short summary, with … for the raw stretch. */
function SpokenRequest({ entry }: { entry: ChatEntry }) {
  const [open, setOpen] = useState(false);
  const id = useId();
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
              onClick={() => setOpen((value) => !value)}
            >
              …
            </button>
          </>
        )}
      </p>
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
};

function replyPlaceholder(entry: ChatEntry): string | null {
  if (entry.status === "sending") return "Enviando…";
  if (entry.status === "queued") return "En cola…";
  if (entry.status === "running") return RUNNING[entry.kind] ?? "El asistente está pensando…";
  return null;
}

/** A source named in the chat, opening it in Recursos when it can. */
function SourceButton({ sourceId, onOpenSource, text }: { sourceId: string; onOpenSource?: OpenSource; text?: string }) {
  const name = text ?? sourceName(sourceId);
  const item = sourceItem(sourceId);
  if (onOpenSource === undefined || item === null) return <>{name}</>;
  return (
    <button
      type="button"
      className="ws-chat-ref"
      aria-label={`Ver la fuente: ${sourceName(sourceId)}`}
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
  return count === 1 ? "Ha surgido 1 duda: te la pregunto aquí." : `Han surgido ${count} dudas: te las pregunto aquí, de una en una.`;
}

const DOUBT_BADGE: Record<string, string> = {
  open: "Duda",
  resolved: "Duda resuelta",
  auto_resolved: "Duda resuelta",
  dismissed: "Duda descartada",
};

/**
 * A doubt asked in the chat (#325): highlighted, with the question, the suggestions as a
 * numbered list and, for a contradiction, each option with its source (which opens in
 * Recursos). The student answers by typing or saying it; `doubt.resolved` marks it answered.
 */
function DoubtEntry({ doubt, onOpenSource }: { doubt: DoubtView; onOpenSource?: OpenSource }) {
  const open = doubt.status === "open";
  const withOptions = new Set(doubt.options.map((option) => option.sourceId));
  const others = doubt.refs.filter((ref) => !withOptions.has(ref));
  return (
    <div className={open ? "ws-chat-doubt" : "ws-chat-doubt ws-chat-doubt-closed"}>
      <p className="ws-chat-who">
        Asistente <span className="ws-chat-badge">{DOUBT_BADGE[doubt.status] ?? "Duda"}</span>
      </p>
      <p className="ws-chat-text">{doubt.question}</p>
      {doubt.suggestions.length > 0 && (
        <ol className="ws-chat-suggestions" aria-label="Sugerencias">
          {doubt.suggestions.map((suggestion, index) => (
            <li key={index}>{suggestion}</li>
          ))}
        </ol>
      )}
      {doubt.options.length > 0 && (
        <ul className="ws-chat-options" aria-label="Qué dice cada fuente">
          {doubt.options.map((option) => (
            <li key={option.sourceId}>
              <SourceButton sourceId={option.sourceId} onOpenSource={onOpenSource} text={capitalized(sourceName(option.sourceId))} />
              : «{option.says}»
            </li>
          ))}
        </ul>
      )}
      {others.length > 0 && (
        <p className="ws-chat-refs">
          Sobre: <Sources sourceIds={others} onOpenSource={onOpenSource} />
        </p>
      )}
      {open && <p className="ws-chat-hint">Contesta escribiendo o de viva voz: «la 2», «pone “escrita”»…</p>}
      {!open && doubt.answer !== null && <p className="ws-chat-answer">Respondiste: «{doubt.answer}»</p>}
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
  /** A whole-topic run: its batches, shown below its progress. */
  batches?: ReactNode;
}

function Reply({ entry, versionsPath, onOpenSource, onRetry, idle, batches }: ReplyProps) {
  const [showDiff, setShowDiff] = useState(false);
  const diffId = useId();
  const placeholder = replyPlaceholder(entry);
  const text = entry.reply !== "" ? entry.reply : placeholder;
  if (entry.kind === "doubt" && entry.doubt !== null) return <DoubtEntry doubt={entry.doubt} onOpenSource={onOpenSource} />;
  if (entry.kind === "doubts_resolved") return <p className="ws-chat-line">{entry.reply}</p>;
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
          <p className={entry.reply === "" ? "ws-chat-text ws-chat-waiting" : "ws-chat-text"}>{text}</p>
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
 * highlighted, and answered by typing or saying it. Below, a textarea (Enter sends, Shift+Enter
 * is a new line) with **Enviar** and "Deshacer el último cambio".
 */
export default function ChatPanel({
  chat,
  versionsPath,
  onOpenSource,
}: {
  chat: WorkspaceChat;
  /** The topic's versions page, where a change read from the history is compared. */
  versionsPath: string;
  onOpenSource?: OpenSource;
}) {
  const [draft, setDraft] = useState("");
  const idle = chat.busy === null;
  const keys = new Set(chat.entries.map((entry) => entry.key));
  const top = chat.entries.filter((entry) => entry.parent === null || !keys.has(entry.parent));
  const children = new Map<string, ChatEntry[]>();
  for (const entry of chat.entries) {
    if (entry.parent !== null && keys.has(entry.parent)) children.set(entry.parent, [...(children.get(entry.parent) ?? []), entry]);
  }

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!idle || draft.trim() === "") return;
    chat.send(draft);
    setDraft("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
  };

  return (
    <section className="ws-chat" aria-labelledby="ws-chat-heading">
      <h2 id="ws-chat-heading" className="ws-chat-heading">
        Chat con el asistente
      </h2>
      {chat.connection === "down" && (
        <p className="ws-chat-status" role="status">
          Sin conexión en directo con el asistente; reintentando…
        </p>
      )}
      {chat.historyFailure !== null && <p role="alert">{chat.historyFailure}</p>}
      {chat.entries.length === 0 && chat.historyFailure === null && (
        <p className="ws-chat-hint">
          Pídele cambios al asistente hablando o escribiendo: «pon esto como definición», «haz una tabla con las tres
          causas»…
        </p>
      )}
      <ol className="ws-chat-log" role="log" aria-live="polite" aria-label="Conversación con el asistente">
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
                batches={
                  own.length > 0 && (
                    <ol className="ws-chat-batches" aria-label="Tandas de la preparación">
                      {own.map((child) => (
                        <li key={child.key} className="ws-chat-batch" aria-busy={child.status === "running" ? "true" : undefined}>
                          <Reply entry={child} versionsPath={versionsPath} onOpenSource={onOpenSource} onRetry={chat.retry} idle={idle} />
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
      <div aria-live="polite">
        {chat.busy === "undo" && <p>Deshaciendo el último cambio…</p>}
        {chat.notice !== null && <p>{chat.notice}</p>}
      </div>
      <form className="ws-chat-form" onSubmit={submit}>
        <label htmlFor="ws-chat-input" className="ws-chat-label">
          Mensaje para el asistente
        </label>
        <textarea
          id="ws-chat-input"
          rows={2}
          maxLength={4000}
          placeholder={chat.doubtAsked ? "Responde a la duda o escribe otra cosa…" : "Escribe o habla: «pon un ejemplo aquí»…"}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="ws-chat-actions">
          <button type="submit" disabled={!idle || draft.trim() === ""}>
            Enviar
          </button>
          <button type="button" onClick={chat.undo} disabled={!idle || !chat.canUndo}>
            Deshacer el último cambio
          </button>
        </div>
      </form>
    </section>
  );
}
