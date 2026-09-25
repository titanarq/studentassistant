import { type FormEvent, type KeyboardEvent, useState } from "react";
import DiffView from "./DiffView";
import type { EditorChat as Chat, ChatEntry } from "./useEditorChat";
import "./chat.css";

/** Opens the sources panel on a footnote label; `trigger` gets the focus back on close. */
export type OpenSource = (label: string, trigger: HTMLElement) => void;

function Entry({ entry, onOpenSource }: { entry: ChatEntry; onOpenSource?: OpenSource }) {
  return (
    <li className="chat-entry" aria-busy={entry.streaming ? "true" : undefined}>
      <p className="chat-message">
        <span className="chat-who">Tú:</span> {entry.message}
      </p>
      <div className="chat-reply">
        <span className="chat-who">Editor:</span>{" "}
        {entry.reply !== "" ? entry.reply : entry.streaming ? "El editor está pensando…" : null}
      </div>
      {entry.refs.length > 0 && (
        <p className="chat-refs">
          Fuentes:{" "}
          {entry.refs.map((ref, index) => (
            <span key={`${ref.label}-${index}`}>
              {index > 0 && ", "}
              {onOpenSource !== undefined ? (
                <button
                  type="button"
                  className="chat-ref"
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
      {entry.applied && entry.summary !== null && (
        <p className="chat-applied">
          {entry.undone ? "Cambio deshecho" : "Cambio aplicado"}: {entry.summary}
        </p>
      )}
      {entry.warning !== null && <p className="chat-warning">{entry.warning}</p>}
      {entry.failure !== null && (
        <p role="alert" className="chat-warning">
          No se pudo completar: {entry.failure}
        </p>
      )}
      {entry.diff !== null && !entry.undone && <DiffView diff={entry.diff} />}
    </li>
  );
}

/**
 * The chat with the editor beside the notes (VISION §4 step 8): the conversation, the reply of the
 * running turn as it streams, the diff each turn applied, "Deshacer el último cambio" and, when a
 * turn stopped at the cost cap, "Continuar igualmente". Enter sends; Shift+Enter is a new line.
 * A "¿Por qué pusiste esto?" answer lists the block's sources; `onOpenSource` opens one in the
 * sources panel.
 */
export default function EditorChat({ chat, onOpenSource }: { chat: Chat; onOpenSource?: OpenSource }) {
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
    <section className="editor-chat" aria-labelledby="editor-chat-heading">
      <h2 id="editor-chat-heading">Hablar con el editor</h2>
      {chat.historyFailure !== null && <p role="alert">{chat.historyFailure}</p>}
      {chat.entries.length === 0 && chat.historyFailure === null && (
        <p className="chat-hint">
          Dile qué cambiar: «esto está demasiado resumido», «pon un ejemplo», «no inventes», «usa la explicación del
          libro»…
        </p>
      )}
      <ol className="chat-log" role="log" aria-label="Conversación con el editor">
        {chat.entries.map((entry) => (
          <Entry key={entry.key} entry={entry} onOpenSource={onOpenSource} />
        ))}
      </ol>
      <div aria-live="polite">
        {chat.busy === "undo" && <p>Deshaciendo el último cambio…</p>}
        {chat.notice !== null && <p>{chat.notice}</p>}
      </div>
      {chat.overCap && idle && (
        <button type="button" onClick={chat.retry}>
          Continuar igualmente
        </button>
      )}
      <form className="chat-form" onSubmit={submit}>
        <label htmlFor="editor-chat-input">Mensaje para el editor</label>
        <textarea
          id="editor-chat-input"
          rows={3}
          maxLength={4000}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="chat-actions">
          <button type="submit" disabled={!idle || draft.trim() === ""}>
            {chat.busy === "send" ? "El editor está respondiendo…" : "Enviar"}
          </button>
          <button type="button" onClick={chat.undo} disabled={!idle || !chat.canUndo}>
            Deshacer el último cambio
          </button>
        </div>
      </form>
    </section>
  );
}
