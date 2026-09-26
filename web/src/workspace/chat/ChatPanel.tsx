import { type FormEvent, type KeyboardEvent, useId, useState } from "react";
import DiffView from "../../chat/DiffView";
import type { OpenSource } from "../../chat/EditorChat";
import type { SpokenSpan } from "./api";
import type { ChatEntry } from "./turns";
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

function TypedRequest({ entry }: { entry: ChatEntry }) {
  return (
    <div className="ws-chat-request">
      <p className="ws-chat-who">{entry.kind === "explain" ? "Preguntaste" : "Escribiste"}</p>
      <p className="ws-chat-said">{entry.message ?? "Un mensaje escrito"}</p>
    </div>
  );
}

/** How the request of a turn shows; one place per kind, so later kinds (#329) add a case. */
function Request({ entry }: { entry: ChatEntry }) {
  switch (entry.origin) {
    case "voice":
      return <SpokenRequest entry={entry} />;
    case "typed":
      return <TypedRequest entry={entry} />;
  }
}

function replyPlaceholder(entry: ChatEntry): string | null {
  if (entry.status === "queued") return "En cola…";
  if (entry.status === "running") return entry.kind === "prepare_notes" ? "Preparando los apuntes del tema…" : "El asistente está pensando…";
  return null;
}

interface ReplyProps {
  entry: ChatEntry;
  versionsPath: string;
  onOpenSource?: OpenSource;
  onRetry: (key: string) => void;
  idle: boolean;
}

function Reply({ entry, versionsPath, onOpenSource, onRetry, idle }: ReplyProps) {
  const [showDiff, setShowDiff] = useState(false);
  const diffId = useId();
  const placeholder = replyPlaceholder(entry);
  const text = entry.reply !== "" ? entry.reply : placeholder;
  return (
    <div className="ws-chat-reply">
      {(text !== null || entry.status !== "failed") && (
        <>
          <p className="ws-chat-who">Asistente</p>
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
      {entry.warning !== null && <p className="ws-chat-warning">{entry.warning}</p>}
      {entry.failure !== null && (
        <div className="ws-chat-failure">
          <p role="alert" className="ws-chat-warning">
            No se pudo completar: {entry.failure}
          </p>
          {entry.overCap && (
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
 * The workspace's chat panel (#317, epic #311): each request as a short line -- a spoken one as
 * "Pediste: <summary>" with **…** to show the raw transcript of that stretch and its time range --,
 * then the assistant's reply as it streams, the applied change with its diff, and failures in
 * Spanish (a reached cost cap offers "Continuar igualmente"). Requests waiting for their turn say
 * "En cola…". Below, a textarea (Enter sends, Shift+Enter is a new line) with **Enviar** and
 * "Deshacer el último cambio".
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
        {chat.entries.map((entry) => (
          <li key={entry.key} className="ws-chat-entry" aria-busy={entry.status === "running" ? "true" : undefined}>
            <Request entry={entry} />
            <Reply entry={entry} versionsPath={versionsPath} onOpenSource={onOpenSource} onRetry={chat.retry} idle={idle} />
          </li>
        ))}
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
          placeholder="Escribe o habla: «pon un ejemplo aquí»…"
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
