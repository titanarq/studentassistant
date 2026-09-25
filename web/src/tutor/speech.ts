/**
 * The tutor's voice (#82): its answers read aloud with the browser's `speechSynthesis`, in Spanish.
 * `spokenText` is what is read -- the answer without the notes' footnote marks and Markdown -- and
 * `browserSpeechOutput` the one place this page reads the synthesis API from, so a test hands the
 * screen a fake `SpeechOutput` instead.
 */

export const SPEECH_OUTPUT_LANGUAGE = "es-ES";

/** Speaks one text at a time: `speak` replaces whatever is being read. */
export interface SpeechOutput {
  readonly supported: boolean;
  /** Reads `text`; `onEnd` runs once when it is done, stopped or failed. */
  speak(text: string, onEnd?: () => void): void;
  /** Stops reading at once. */
  cancel(): void;
}

const FOOTNOTE_REF = /\[\^[^\]\s]+\](?!:)/g;
const UNCERTAIN = /\[\[\?([^\]]*)\]\]/g;
const EMPHASIS = /(\*\*|__|\*|_|`|\$)/g;
const HEADING = /^#{1,6}\s+/gm;

/** The answer as it is read aloud: no `[^label]` marks, `[[?..]]` doubts or Markdown symbols. */
export function spokenText(reply: string): string {
  return reply
    .replace(FOOTNOTE_REF, "")
    .replace(UNCERTAIN, "$1")
    .replace(HEADING, "")
    .replace(EMPHASIS, "")
    .replace(/\s+([.,;:!?])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

/** The answer as it is shown: each `[^label]` as `[label]`, matching the list of sources. */
export function shownText(reply: string): string {
  return reply.replace(FOOTNOTE_REF, (mark) => `[${mark.slice(2, -1)}]`);
}

interface VoiceLike {
  readonly lang: string;
}

interface SynthesisLike {
  speak(utterance: unknown): void;
  cancel(): void;
  getVoices?(): VoiceLike[];
}

interface UtteranceLike {
  lang: string;
  voice?: unknown;
  onend: (() => void) | null;
  onerror: (() => void) | null;
}

type UtteranceConstructor = new (text: string) => UtteranceLike;

function globalSlot(name: string): unknown {
  const value = (globalThis as Record<string, unknown>)[name];
  if (value !== undefined) return value;
  const scope = (globalThis as unknown as { window?: Record<string, unknown> }).window;
  return scope === undefined ? undefined : scope[name];
}

/** A Spanish voice, Spain's first, or undefined to let the browser choose by `lang`. */
function spanishVoice(synthesis: SynthesisLike): VoiceLike | undefined {
  let voices: VoiceLike[] = [];
  try {
    voices = synthesis.getVoices?.() ?? [];
  } catch {
    voices = [];
  }
  return (
    voices.find((voice) => voice.lang.toLowerCase().replace("_", "-") === "es-es") ??
    voices.find((voice) => voice.lang.toLowerCase().startsWith("es"))
  );
}

/** The browser's `speechSynthesis`, or an unsupported output where there is none. */
export function browserSpeechOutput(): SpeechOutput {
  const synthesis = globalSlot("speechSynthesis") as SynthesisLike | undefined;
  const Utterance = globalSlot("SpeechSynthesisUtterance") as UtteranceConstructor | undefined;
  if (synthesis === undefined || typeof Utterance !== "function") {
    return { supported: false, speak: (_text, onEnd) => onEnd?.(), cancel: () => undefined };
  }
  return {
    supported: true,
    speak(text, onEnd) {
      synthesis.cancel();
      const utterance = new Utterance(text);
      utterance.lang = SPEECH_OUTPUT_LANGUAGE;
      const voice = spanishVoice(synthesis);
      if (voice !== undefined) utterance.voice = voice;
      let ended = false;
      const end = () => {
        if (ended) return;
        ended = true;
        onEnd?.();
      };
      utterance.onend = end;
      utterance.onerror = end;
      synthesis.speak(utterance);
    },
    cancel() {
      synthesis.cancel();
    },
  };
}
