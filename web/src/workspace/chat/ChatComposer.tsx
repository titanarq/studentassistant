import { type FormEvent, type KeyboardEvent, type ReactNode, type Ref, useCallback, useEffect, useRef } from "react";
import VoiceInputButton from "../../tutor/VoiceInputButton";
import type { VoiceQuestionStarter } from "../../tutor/voiceQuestion";
import "./chat.css";

export interface ComposerVoice {
  /** The recognized message: the chat puts it in the input and sends it like a typed one. */
  onFinal: (text: string) => void;
  /** No new recognition can start (the chat is busy); a running one is stopped. */
  disabled?: boolean;
  /** Listens for one utterance; the Web Speech API by default. */
  listen?: VoiceQuestionStarter;
  /** Whether `listen` can work here; asked of the browser by default. */
  voiceSupported?: boolean;
}

export interface ChatComposerProps {
  /** The textarea's id; its label is `label` (on screen only outside the two-column frame). */
  id: string;
  label: string;
  placeholder: string;
  value: string;
  onChange: (text: string) => void;
  /** **Enviar** or Enter (Shift+Enter is a new line). */
  onSubmit: () => void;
  /** Whether **Enviar** can be pressed. */
  canSubmit: boolean;
  submitLabel?: string;
  maxLength: number;
  /** The textarea is disabled (the study chat while an answer comes). */
  disabled?: boolean;
  inputRef?: Ref<HTMLTextAreaElement>;
  /** The microphone button (#428, #458), or none (a running capture hears the student). */
  voice?: ComposerVoice | null;
  /** Above the textarea: the Recursos selection's chips (#432). */
  before?: ReactNode;
  /** More icon buttons after the microphone («Deshacer el último cambio»). */
  actions?: ReactNode;
}

/**
 * The input of a chat card (#458, shared by the Construir and Estudiar chats since #487): a
 * textarea (Enter sends, Shift+Enter is a new line) with, in one row below it, the send button and
 * the square icon buttons -- the microphone (`VoiceInputButton` with its icon, the Web Speech path
 * of #428) and whatever `actions` the chat adds. The textarea grows with its text up to a limit
 * (`chat.css`), and text written while it is not focused (dictation) keeps its end in view.
 */
export default function ChatComposer({
  id,
  label,
  placeholder,
  value,
  onChange,
  onSubmit,
  canSubmit,
  submitLabel = "Enviar",
  maxLength,
  disabled = false,
  inputRef,
  voice = null,
  before,
  actions,
}: ChatComposerProps) {
  const textarea = useRef<HTMLTextAreaElement | null>(null);
  const setTextarea = useCallback(
    (element: HTMLTextAreaElement | null) => {
      textarea.current = element;
      if (typeof inputRef === "function") inputRef(element);
      else if (inputRef) inputRef.current = element;
    },
    [inputRef],
  );
  // Dictated text arrives while the microphone button has the focus: follow it to its end.
  useEffect(() => {
    const element = textarea.current;
    if (element !== null && element.ownerDocument.activeElement !== element) element.scrollTop = element.scrollHeight;
  }, [value]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit();
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
  };
  return (
    <form className="ws-chat-form" onSubmit={submit}>
      <label htmlFor={id} className="ws-chat-label">
        {label}
      </label>
      {before}
      <textarea
        id={id}
        ref={setTextarea}
        rows={2}
        maxLength={maxLength}
        placeholder={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
        disabled={disabled}
      />
      <div className="ws-chat-actions">
        <button type="submit" disabled={!canSubmit}>
          {submitLabel}
        </button>
        {voice !== null && (
          <VoiceInputButton
            icon
            value={value}
            onChange={onChange}
            onFinal={voice.onFinal}
            disabled={voice.disabled}
            listen={voice.listen}
            voiceSupported={voice.voiceSupported}
          />
        )}
        {actions}
      </div>
    </form>
  );
}
