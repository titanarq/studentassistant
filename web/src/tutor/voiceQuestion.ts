/**
 * One spoken question (#82): a single Web Speech recognition in Spanish, not continuous, with its
 * interim text, which ends when the student stops talking (or `stop()` is called) and hands over
 * the final text. The capture page's continuous transcriber (`capture/webSpeechTranscriber.ts`) is
 * the other use of the API; this one never talks to a session. The browser writes Spanish without
 * punctuation, so the final text gets a minimal one (`punctuate`).
 */

import { speechRecognitionConstructor, WEB_SPEECH_LANGUAGE } from "../capture/webSpeechTranscriber";

export type VoiceProblemCode = "unsupported" | "permission-denied" | "no-speech" | "network" | "unavailable";

export interface VoiceQuestionCallbacks {
  /** The question so far, while the student speaks. */
  onInterim?: (text: string) => void;
  /** The recognized question; not called when nothing was understood. */
  onFinal: (text: string) => void;
  /** Why no question came out. */
  onProblem: (code: VoiceProblemCode) => void;
  /** The recognition is over, whatever the outcome. */
  onEnd?: () => void;
}

/** A running recognition: `stop()` ends it and keeps what was said so far. */
export interface VoiceQuestionHandle {
  stop(): void;
}

export type VoiceQuestionStarter = (callbacks: VoiceQuestionCallbacks) => VoiceQuestionHandle;

interface Alternative {
  readonly transcript: string;
}

interface Result extends ArrayLike<Alternative> {
  readonly isFinal: boolean;
}

interface Recognition {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  onresult: ((event: { results: ArrayLike<Result> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: ((event: Event) => void) | null;
  start(): void;
  stop(): void;
}

const PROBLEMS: Record<string, VoiceProblemCode> = {
  "not-allowed": "permission-denied",
  "service-not-allowed": "permission-denied",
  "no-speech": "no-speech",
  network: "network",
};

/** First words that open a question (accented: the browser writes «como» for the conjunction). */
const QUESTION_WORDS = new Set([
  "qué", "cómo", "cuándo", "dónde", "adónde", "cuál", "cuáles", "quién", "quiénes", "cuánto",
  "cuánta", "cuántos", "cuántas", "puedes", "podrías", "sabes",
]);
/** First words that open a question only when followed by one of these («por qué», «me explicas»). */
const QUESTION_PAIRS: Record<string, ReadonlySet<string>> = {
  por: new Set(["qué"]),
  me: new Set(["explicas", "puedes", "podrías", "dices", "cuentas"]),
};

/**
 * `text` with a minimal Spanish punctuation (the Web Speech API writes none): a capital first
 * letter and a final full stop, or «¿…?» when it opens with a question word («qué», «cómo»,
 * «por qué», «puedes»…); deliberately conservative, a statement never becomes a question. Text
 * that already ends in punctuation only gets its capital.
 */
export function punctuate(text: string): string {
  const trimmed = text.trim().replace(/\s+/g, " ");
  if (trimmed === "") return trimmed;
  const capital = (value: string) => value.charAt(0).toLocaleUpperCase("es") + value.slice(1);
  if (/[.!?…:;]$/.test(trimmed)) return capital(trimmed);
  const [first, second = ""] = trimmed.toLocaleLowerCase("es").split(" ");
  const pair = QUESTION_PAIRS[first];
  const question = pair !== undefined ? pair.has(second) : QUESTION_WORDS.has(first);
  return question ? `¿${capital(trimmed)}?` : `${capital(trimmed)}.`;
}

export function voiceQuestionSupported(): boolean {
  return speechRecognitionConstructor() !== null;
}

/** Starts listening for one question (see the module comment). */
export const listenForQuestion: VoiceQuestionStarter = (callbacks) => {
  const Constructor = speechRecognitionConstructor();
  if (Constructor === null) {
    callbacks.onProblem("unsupported");
    callbacks.onEnd?.();
    return { stop: () => undefined };
  }
  const recognition = new Constructor() as unknown as Recognition;
  recognition.lang = WEB_SPEECH_LANGUAGE;
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;
  let settled = "";
  let problem: VoiceProblemCode | null = null;
  let over = false;

  recognition.onresult = (event) => {
    const finals: string[] = [];
    const interims: string[] = [];
    for (let index = 0; index < event.results.length; index += 1) {
      const result = event.results[index];
      const text = result.length > 0 ? result[0].transcript.trim() : "";
      if (text === "") continue;
      (result.isFinal ? finals : interims).push(text);
    }
    settled = finals.join(" ");
    callbacks.onInterim?.([...finals, ...interims].join(" "));
  };
  recognition.onerror = (event) => {
    if (event.error === "aborted") return;
    problem ??= PROBLEMS[event.error] ?? "unavailable";
  };
  recognition.onend = () => {
    if (over) return;
    over = true;
    if (settled !== "") callbacks.onFinal(punctuate(settled));
    else callbacks.onProblem(problem ?? "no-speech");
    callbacks.onEnd?.();
  };
  try {
    recognition.start();
  } catch {
    over = true;
    callbacks.onProblem("unavailable");
    callbacks.onEnd?.();
  }
  return {
    stop() {
      if (!over) recognition.stop();
    },
  };
};
