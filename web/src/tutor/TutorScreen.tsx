/**
 * The voice tutor (#82) on the capture page: the student asks about one topic out loud (or types)
 * and the tutor answers from the topic's notes and sources, streamed, with the sources it cites,
 * and reads the answer aloud. No capture session is involved: it only reads the topic.
 */

import { type FormEvent, type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import type { ChatRef } from "../chat/api";
import { describeFailure, topicPath } from "../desk/api";
import { askTutor, describeTutorFailure, fetchTutorHistory, type TutorTurn } from "./api";
import { browserSpeechOutput, type SpeechOutput, shownText, spokenText } from "./speech";
import {
  listenForQuestion,
  type VoiceProblemCode,
  type VoiceQuestionHandle,
  type VoiceQuestionStarter,
  voiceQuestionSupported,
} from "./voiceQuestion";
import "../chat/chat.css";
import "./tutor.css";

export const MAX_QUESTION_CHARS = 1000;

export interface TutorScreenProps {
  subjectId: string;
  topicId: string;
  subjectName: string;
  topicName: string;
  /** Back to the topic picker. */
  onClose?: () => void;
  /** Reads the answers aloud; the browser's `speechSynthesis` by default. */
  speech?: SpeechOutput;
  /** Listens for one spoken question; the Web Speech API by default. */
  listen?: VoiceQuestionStarter;
  /** Whether `listen` can work here; asked of the browser by default. */
  voiceSupported?: boolean;
}

export const VOICE_PROBLEMS: Record<VoiceProblemCode, string> = {
  unsupported: "Este navegador no reconoce la voz: escribe tu pregunta.",
  "permission-denied":
    "No hay permiso para usar el micrófono: permítelo en el navegador o escribe tu pregunta.",
  "no-speech": "No te he oído. Pulsa «Preguntar por voz» y habla.",
  network:
    "El reconocimiento de voz necesita conexión a internet: escribe tu pregunta o inténtalo de nuevo.",
  unavailable: "El reconocimiento de voz no está disponible ahora mismo: escribe tu pregunta.",
};

type History = { state: "loading" } | { state: "ready" } | { state: "failed"; message: string };

interface Asking {
  question: string;
  reply: string;
}

interface Failure {
  message: string;
  /** The question a "Continuar igualmente" repeats past the cost cap. */
  overCapQuestion: string | null;
}

function Refs({ refs }: { refs: ChatRef[] }) {
  if (refs.length === 0) return null;
  return (
    <div className="chat-refs">
      Fuentes:
      <ul>
        {refs.map((ref) => (
          <li key={ref.label}>
            [{ref.label}] {ref.text}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function TutorScreen({
  subjectId,
  topicId,
  subjectName,
  topicName,
  onClose,
  speech: givenSpeech,
  listen = listenForQuestion,
  voiceSupported: givenVoiceSupported,
}: TutorScreenProps) {
  const [speech] = useState<SpeechOutput>(() => givenSpeech ?? browserSpeechOutput());
  const [voiceSupported] = useState(() => givenVoiceSupported ?? voiceQuestionSupported());
  const [history, setHistory] = useState<History>({ state: "loading" });
  const [turns, setTurns] = useState<TutorTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [asking, setAsking] = useState<Asking | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [listening, setListening] = useState(false);
  const [heard, setHeard] = useState("");
  const [voiceProblem, setVoiceProblem] = useState<string | null>(
    voiceSupported ? null : VOICE_PROBLEMS.unsupported,
  );
  const [readAloud, setReadAloud] = useState(speech.supported);
  const [speaking, setSpeaking] = useState(false);
  const recognition = useRef<VoiceQuestionHandle | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      recognition.current?.stop();
      speech.cancel();
    };
  }, [speech]);

  useEffect(() => {
    let cancelled = false;
    fetchTutorHistory(subjectId, topicId).then((result) => {
      if (cancelled) return;
      if (result.kind === "ok") {
        setTurns(result.value);
        setHistory({ state: "ready" });
      } else {
        setHistory({
          state: "failed",
          message: `No se han podido cargar las preguntas anteriores: ${describeFailure(result)}`,
        });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  const stopSpeaking = useCallback(() => {
    speech.cancel();
    setSpeaking(false);
  }, [speech]);

  const ask = useCallback(
    async (question: string, confirmOverCap = false) => {
      const text = question.trim();
      if (text === "" || asking !== null) return;
      stopSpeaking();
      setFailure(null);
      setAsking({ question: text, reply: "" });
      const outcome = await askTutor(subjectId, topicId, text, {
        confirmOverCap,
        onDelta: (delta) => {
          if (mounted.current) setAsking((now) => (now === null ? now : { ...now, reply: now.reply + delta }));
        },
      });
      if (!mounted.current) return;
      setAsking(null);
      if (outcome.kind === "ok") {
        const answer = outcome.value;
        setTurns((now) => [...now, { ...answer, time: new Date().toISOString() }]);
        setDraft("");
        const spoken = spokenText(answer.reply);
        if (readAloud && speech.supported && spoken !== "") {
          setSpeaking(true);
          speech.speak(spoken, () => {
            if (mounted.current) setSpeaking(false);
          });
        }
        return;
      }
      setDraft(text);
      setFailure({
        message: describeTutorFailure(outcome),
        overCapQuestion: outcome.kind === "refused" && outcome.overCap ? text : null,
      });
    },
    [asking, readAloud, speech, stopSpeaking, subjectId, topicId],
  );

  const startListening = useCallback(() => {
    if (listening || asking !== null) return;
    stopSpeaking();
    setVoiceProblem(null);
    setHeard("");
    setListening(true);
    recognition.current = listen({
      onInterim: (text) => {
        if (mounted.current) setHeard(text);
      },
      onFinal: (text) => {
        if (!mounted.current) return;
        setHeard("");
        setDraft(text);
        void ask(text);
      },
      onProblem: (code) => {
        if (mounted.current) setVoiceProblem(VOICE_PROBLEMS[code]);
      },
      onEnd: () => {
        recognition.current = null;
        if (mounted.current) setListening(false);
      },
    });
  }, [ask, asking, listen, listening, stopSpeaking]);

  const stopListening = useCallback(() => recognition.current?.stop(), []);

  function submit(event: FormEvent) {
    event.preventDefault();
    void ask(draft);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void ask(draft);
    }
  }

  const busy = asking !== null;

  return (
    <main className="tutor-page">
      <p className="crumbs">
        <button type="button" onClick={onClose}>
          ← Volver
        </button>
      </p>
      <h1>Preguntar al tutor</h1>
      <p className="page-context">
        {subjectName} · {topicName}. El tutor contesta con tus apuntes y tus fuentes.{" "}
        <a href={`${topicPath(subjectId, topicId)}/notes`}>Ver los apuntes del tema</a>
      </p>

      <section className="tutor-conversation" aria-label="Conversación con el tutor">
        {history.state === "loading" && <p role="status">Cargando las preguntas anteriores…</p>}
        {history.state === "failed" && <p role="alert">{history.message}</p>}
        {history.state === "ready" && turns.length === 0 && !busy && (
          <p>Todavía no le has preguntado nada sobre este tema.</p>
        )}
        <ol className="chat-log" aria-live="polite">
          {turns.map((turn, index) => (
            <li className="chat-entry" key={`${turn.time}-${index}`}>
              <p className="chat-message">
                <span className="chat-who">Tú:</span> {turn.question}
              </p>
              <div className="chat-reply">
                <span className="chat-who">Tutor:</span> {shownText(turn.reply)}
              </div>
              <Refs refs={turn.refs} />
              {turn.warning !== null && <p className="chat-warning">{turn.warning}</p>}
            </li>
          ))}
          {asking !== null && (
            <li className="chat-entry" aria-busy="true">
              <p className="chat-message">
                <span className="chat-who">Tú:</span> {asking.question}
              </p>
              <div className="chat-reply">
                <span className="chat-who">Tutor:</span>{" "}
                {asking.reply !== "" ? shownText(asking.reply) : "El tutor está pensando…"}
              </div>
            </li>
          )}
        </ol>
        {failure !== null && (
          <div role="alert">
            <p>{failure.message}</p>
            {failure.overCapQuestion !== null && (
              <button type="button" disabled={busy} onClick={() => void ask(failure.overCapQuestion ?? "", true)}>
                Continuar igualmente
              </button>
            )}
          </div>
        )}
      </section>

      <section className="tutor-ask" aria-label="Tu pregunta">
        <p className="tutor-voice">
          <button
            type="button"
            aria-pressed={listening}
            disabled={!voiceSupported || busy}
            onClick={listening ? stopListening : startListening}
          >
            {listening ? "Escuchando… (pulsa para terminar)" : "Preguntar por voz"}
          </button>
        </p>
        {listening && heard !== "" && (
          <p role="status" aria-label="Lo que te oigo">
            {heard}
          </p>
        )}
        {voiceProblem !== null && <p role="alert">{voiceProblem}</p>}
        <form className="tutor-form" onSubmit={submit}>
          <label>
            Escribe tu pregunta
            <textarea
              value={draft}
              maxLength={MAX_QUESTION_CHARS}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              disabled={busy}
            />
          </label>
          <button type="submit" disabled={busy || draft.trim() === ""}>
            Preguntar
          </button>
        </form>
        <p>
          <label>
            <input
              type="checkbox"
              checked={readAloud}
              disabled={!speech.supported}
              onChange={(event) => {
                setReadAloud(event.target.checked);
                if (!event.target.checked) stopSpeaking();
              }}
            />{" "}
            Leer las respuestas en voz alta
          </label>{" "}
          {speaking && (
            <button type="button" onClick={stopSpeaking}>
              Parar de leer
            </button>
          )}
        </p>
        {!speech.supported && <p>Este navegador no puede leer en voz alta: las respuestas se muestran escritas.</p>}
      </section>
    </main>
  );
}
