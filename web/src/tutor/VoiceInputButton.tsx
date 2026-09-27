/**
 * The microphone button of a chat input (#428): one press, one spoken message, through the same
 * one-utterance Web Speech helper as the voice tutor (`voiceQuestion.ts`). While the student
 * speaks, the interim text goes into the chat's input (`onChange`); the final text is handed to
 * `onFinal`, which puts it in the input and sends it the way typing would. Pressing again stops
 * the recognition and keeps what was said so far. A problem shows one Spanish line after the
 * button, cleared on the next press or when the student types; an unsupported browser gets a
 * disabled button with a hint. Unmounting (or `disabled` turning true) stops a running recognition;
 * what it hears after unmounting is dropped.
 */

import { useEffect, useId, useRef, useState } from "react";
import {
  listenForQuestion,
  type VoiceProblemCode,
  type VoiceQuestionHandle,
  type VoiceQuestionStarter,
  voiceQuestionSupported,
} from "./voiceQuestion";
import "./voiceInput.css";

/** The line each problem shows next to a chat input. */
export const CHAT_VOICE_PROBLEMS: Record<VoiceProblemCode, string> = {
  unsupported: "Este navegador no reconoce la voz: escribe tu mensaje.",
  "permission-denied": "No hay permiso para usar el micrófono: permítelo en el navegador o escribe tu mensaje.",
  "no-speech": "No te he oído. Pulsa «Hablar» y habla.",
  network: "El reconocimiento de voz necesita conexión a internet: escribe tu mensaje o inténtalo de nuevo.",
  unavailable: "El reconocimiento de voz no está disponible ahora mismo: escribe tu mensaje.",
};

export interface VoiceInputButtonProps {
  /** The chat input's current text. */
  value: string;
  /** Puts text in the chat input: the interim text, or the text from before when nothing came out. */
  onChange: (text: string) => void;
  /** The recognized message: the chat puts it in its input and sends it like a typed one. */
  onFinal: (text: string) => void;
  /** No new recognition can start (the chat is busy); a running one is stopped. */
  disabled?: boolean;
  /** Listens for one utterance; the Web Speech API by default. */
  listen?: VoiceQuestionStarter;
  /** Whether `listen` can work here; asked of the browser by default. */
  voiceSupported?: boolean;
}

export default function VoiceInputButton({
  value,
  onChange,
  onFinal,
  disabled = false,
  listen = listenForQuestion,
  voiceSupported: givenVoiceSupported,
}: VoiceInputButtonProps) {
  const [voiceSupported] = useState(() => givenVoiceSupported ?? voiceQuestionSupported());
  const [listening, setListening] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const hint = useId();
  const recognition = useRef<VoiceQuestionHandle | null>(null);
  const mounted = useRef(true);
  // The latest props, for callbacks that arrive after a render.
  const latest = useRef({ value, onChange, onFinal });
  latest.current = { value, onChange, onFinal };
  // The input text this button last wrote: any other text means the student typed.
  const written = useRef<string | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      recognition.current?.stop();
      recognition.current = null;
    };
  }, []);

  useEffect(() => {
    if (disabled) recognition.current?.stop();
  }, [disabled]);

  useEffect(() => {
    if (problem !== null && written.current !== null && value !== written.current) setProblem(null);
  }, [problem, value]);

  const write = (text: string) => {
    written.current = text;
    latest.current.onChange(text);
  };

  const start = () => {
    if (listening || disabled) return;
    setProblem(null);
    const before = latest.current.value;
    let heard = false;
    setListening(true);
    recognition.current = listen({
      onInterim: (text) => {
        if (!mounted.current) return;
        heard = true;
        write(text);
      },
      onFinal: (text) => {
        if (!mounted.current) return;
        written.current = null;
        latest.current.onFinal(text);
      },
      onProblem: (code) => {
        if (!mounted.current) return;
        if (heard) write(before);
        else written.current = latest.current.value;
        setProblem(CHAT_VOICE_PROBLEMS[code]);
      },
      onEnd: () => {
        recognition.current = null;
        if (mounted.current) setListening(false);
      },
    });
  };

  const stop = () => recognition.current?.stop();
  const unsupportedHint = CHAT_VOICE_PROBLEMS.unsupported;

  return (
    <>
      <button
        type="button"
        className="voice-input-button"
        aria-label={listening ? undefined : "Dictar el mensaje por voz"}
        aria-pressed={listening}
        aria-describedby={voiceSupported ? undefined : hint}
        title={voiceSupported ? undefined : unsupportedHint}
        disabled={!voiceSupported || (disabled && !listening)}
        onClick={listening ? stop : start}
      >
        {listening ? "Escuchando… (pulsa para parar)" : "Hablar"}
      </button>
      {!voiceSupported ? (
        <span id={hint} className="voice-input-line">
          {unsupportedHint}
        </span>
      ) : (
        problem !== null && (
          <span className="voice-input-line" role="alert">
            {problem}
          </span>
        )
      )}
    </>
  );
}
