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

const MARKDOWN_LINK = /!?\[([^\]]*)\]\((?:[^()\s]|\([^()\s]*\))*\)/g;
const BARE_URL = /\b(?:https?:\/\/|www\.)[^\s)]*[^\s).,;:!?]/gi;
const QUOTE = /^\s{0,3}>\s?/gm;
const BULLET = /^\s*[-*+•]\s+/;
const NUMBERED = /^\s*(\d{1,2})[.)]\s+/;
const ORDINALS = ["primero", "segundo", "tercero", "cuarto", "quinto", "sexto", "séptimo", "octavo", "noveno", "décimo"];

/** A list item as a sentence: the marker goes, "1." becomes "Primero, ...", the item ends in a full stop. */
function spokenLine(line: string): string {
  const numbered = NUMBERED.exec(line);
  let text = line.replace(BULLET, "").replace(NUMBERED, "").trim();
  if (text === "") return "";
  const ordinal = numbered === null ? undefined : ORDINALS[Number(numbered[1]) - 1];
  if (ordinal !== undefined) text = `${ordinal[0].toUpperCase()}${ordinal.slice(1)}, ${text[0].toLowerCase()}${text.slice(1)}`;
  return /[.!?:;…]$/.test(text) ? text : `${text}.`;
}

/**
 * The answer as it is read aloud, humanized: no `[^label]` marks, `[[?..]]` doubts or Markdown
 * symbols; a link is read as its text, a bare address as "un enlace"; list items become sentences
 * ("Primero, ...") instead of "guion" or "uno punto". The tutor's prompt already asks the model for
 * spoken prose (no lists, no addresses, formulas in words); this is the safety net for what still
 * slips through.
 */
export function spokenText(reply: string): string {
  return reply
    .replace(FOOTNOTE_REF, "")
    .replace(UNCERTAIN, "$1")
    .replace(MARKDOWN_LINK, "$1")
    .replace(BARE_URL, "un enlace")
    .replace(HEADING, "")
    .replace(QUOTE, "")
    .split("\n")
    .map((line) => (BULLET.test(line) || NUMBERED.test(line) ? spokenLine(line) : line))
    .join("\n")
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
