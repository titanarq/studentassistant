/**
 * One spoken question (#82): a single Web Speech recognition in Spanish, not continuous, with its
 * interim text, which ends when the student stops talking (or `stop()` is called) and hands over
 * the final text. The capture page's continuous transcriber (`capture/webSpeechTranscriber.ts`) is
 * the other use of the API; this one never talks to a session.
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
    if (settled !== "") callbacks.onFinal(settled);
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
