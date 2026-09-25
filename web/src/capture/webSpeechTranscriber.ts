/**
 * The browser's own recognizer as a `ClientTranscriber` (#40, ADR-0008): the Web Speech API, which
 * is what makes the laptop a usable capture client with no server-side STT configured. Two things
 * about it shape this file. A continuous recognition ends by itself -- Chrome stops listening after
 * a stretch of silence -- so every `end` while the session runs starts a fresh recognition, and so
 * does every error it recovers from; the fresh instance is a new one because Chrome's own keeps
 * state a twenty-minute session trips over. And its `results` list numbers utterances by index
 * only within one recognition, so the `segment_id` an utterance is minted with lives in a map that
 * a new recognition starts empty. An interim still pending when a recognition ends is dropped: the
 * recognizer never settled it, and inventing a final would put words in the student's mouth.
 *
 * Since protocol 1.4 (#227) it also biases the recognizer towards the session's vocabulary hints
 * where the browser has contextual biasing: each recognition it starts gets them as its `phrases`
 * (`SpeechRecognitionPhrase`). A browser without that API, or whose service answers
 * `phrases-not-supported` (Chrome does for a cloud recognition, since it biases on-device only),
 * recognizes without them, and the student is told nothing: the hints are a help, not a feature.
 * `SpeechGrammarList` is not used: the specification dropped grammars and no engine applies them.
 */

import { type ClientSegment, type TranscriptKind, WEB_SPEECH_PROVIDER } from "./sessionSocket";
import {
  type ClientTranscriber,
  TranscriberError,
  type TranscriberCallbacks,
  type TranscriberProblem,
  type TranscriberProblemCode,
} from "./transcriber";

/** The language the capture page recognizes: the student speaks Spanish (AGENTS.md). */
export const WEB_SPEECH_LANGUAGE = "es-ES";

/**
 * The pause between two recognitions. Without it a browser that ends as soon as it starts would
 * spin the microphone as fast as it can, which is what a lost audio device looks like.
 */
export const RESTART_DELAY_MS = 300;

/** The two slots a browser offers the Web Speech API in; Chrome only has the prefixed one. */
const SPEECH_GLOBALS = ["SpeechRecognition", "webkitSpeechRecognition"] as const;

/** The contextual-biasing term's constructor, where a browser has one. */
const PHRASE_GLOBAL = "SpeechRecognitionPhrase";

/**
 * The boost every hint gets. The specification's scale runs from 0 (not boosted) to 10 (extremely
 * likely); the hints are the words a student is likely to say, not words to force, so they get a
 * moderate lift, and their order is already the backend's ranking.
 */
export const VOCABULARY_HINT_BOOST = 2.0;

/** The error a recognition with `phrases` reports when its service cannot bias. */
const PHRASES_NOT_SUPPORTED = "phrases-not-supported";

/** One alternative of a result; the recognizer reports no confidence while the text is interim. */
interface SpeechAlternative {
  readonly transcript: string;
  readonly confidence: number;
}

/** One `SpeechRecognitionResult`: the text of one utterance, and whether it is settled. */
interface SpeechResult extends ArrayLike<SpeechAlternative> {
  readonly isFinal: boolean;
}

interface SpeechResultEvent extends Event {
  /** The first result of `results` this event changed; every one before it is already settled. */
  readonly resultIndex: number;
  readonly results: ArrayLike<SpeechResult>;
}

interface SpeechErrorEvent extends Event {
  /** The Web Speech error code: `no-speech`, `not-allowed`, `network`, ... */
  readonly error: string;
  readonly message: string;
}

/**
 * The slice of the Web Speech API this transcriber uses. TypeScript's DOM lib has no binding for
 * it and every browser names its constructor its own way, so the shape is declared here and the
 * global is looked up by name each time a recognition is started.
 */
export interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  onresult: ((event: SpeechResultEvent) => void) | null;
  onerror: ((event: SpeechErrorEvent) => void) | null;
  onend: ((event: Event) => void) | null;
  /** Contextual biasing; absent in a browser without it. */
  phrases?: unknown;
  start(): void;
  abort(): void;
}

export interface SpeechRecognitionConstructor {
  new (): SpeechRecognitionLike;
}

/** `SpeechRecognitionPhrase`: one term and its boost in [0, 10]. */
export interface SpeechRecognitionPhraseConstructor {
  new (phrase: string, boost?: number): unknown;
}

/**
 * A global slot, in the one place this file reads a browser API from. A test environment may keep
 * `window` apart from `globalThis`; a browser never does.
 */
function globalSlot(name: string): unknown {
  const value = (globalThis as Record<string, unknown>)[name];
  if (value !== undefined) return value;
  const scope = (globalThis as unknown as { window?: Record<string, unknown> }).window;
  return scope === undefined ? undefined : scope[name];
}

/** The browser's Web Speech constructor, or null in a browser without the API at all. */
export function speechRecognitionConstructor(): SpeechRecognitionConstructor | null {
  for (const name of SPEECH_GLOBALS) {
    const value = globalSlot(name);
    if (typeof value === "function") return value as SpeechRecognitionConstructor;
  }
  return null;
}

/** The browser's `SpeechRecognitionPhrase`, or null where it has no contextual biasing. */
export function speechRecognitionPhraseConstructor(): SpeechRecognitionPhraseConstructor | null {
  const value = globalSlot(PHRASE_GLOBAL);
  return typeof value === "function" ? (value as SpeechRecognitionPhraseConstructor) : null;
}

/**
 * Whether this browser can transcribe on the client at all. The page asks before it starts a
 * session, so a browser without the API gets its own Spanish explanation instead of a socket that
 * announces a recognizer nobody has.
 */
export function webSpeechSupported(): boolean {
  return speechRecognitionConstructor() !== null;
}

/** What each Web Speech error code means here, and whether the recognition goes on without it. */
interface SpeechErrorMeaning {
  readonly code: TranscriberProblemCode;
  readonly recoverable: boolean;
}

const SPEECH_ERRORS: Record<string, SpeechErrorMeaning> = {
  "not-allowed": { code: "permission-denied", recoverable: false },
  "service-not-allowed": { code: "permission-denied", recoverable: false },
  "audio-capture": { code: "unavailable", recoverable: false },
  "language-not-supported": { code: "unavailable", recoverable: false },
  network: { code: "network", recoverable: true },
};

/**
 * A code this client does not know: reported, so the student sees something, and retried, so the
 * session goes on. Silently dropping it would leave a dead microphone with no explanation, and
 * calling it fatal would end a session over a browser quirk.
 */
const UNKNOWN_SPEECH_ERROR: SpeechErrorMeaning = { code: "unavailable", recoverable: true };

/**
 * The codes a continuous recognition produces on its own: a student who writes in silence for a
 * minute is the normal case of a study session, and `aborted` is what this transcriber's own
 * `stop()` answers with. Neither is worth a message.
 */
const QUIET_SPEECH_ERRORS = new Set(["no-speech", "aborted"]);

function speechProblem(code: string, message: string): TranscriberProblem | null {
  if (QUIET_SPEECH_ERRORS.has(code)) return null;
  const meaning = SPEECH_ERRORS[code] ?? UNKNOWN_SPEECH_ERROR;
  return {
    code: meaning.code,
    detail: message === "" ? code : `${code}: ${message}`,
    recoverable: meaning.recoverable,
  };
}

/** The utterance one result index stands for, of the recognition that is running now. */
interface Utterance {
  readonly segmentId: string;
  /** The client clock when the recognizer first reported this utterance, interim or settled. */
  readonly startMs: number;
}

/**
 * A `segment_id` is the client's own and opaque to the backend (protocol/README.md); it only has
 * to tell this session's utterances apart, and the prefix says which recognizer minted it.
 */
function newSegmentId(): string {
  return `seg-${crypto.randomUUID()}`;
}

export interface WebSpeechTranscriberOptions {
  /** The language to recognize; the capture page's Spanish by default. */
  language?: string;
  /** The vocabulary hints the first recognition starts with (`hello.ack.vocabulary_hints`). */
  vocabularyHints?: readonly string[];
}

export class WebSpeechTranscriber implements ClientTranscriber {
  readonly provider: string;

  private readonly language: string;
  private readonly callbacks: TranscriberCallbacks;
  /** True from `start()` until `stop()` or a fatal problem; the only thing that restarts one. */
  private running = false;
  private recognition: SpeechRecognitionLike | null = null;
  private restart: ReturnType<typeof setTimeout> | null = null;
  /**
   * Result index -> utterance, of the recognition running now. The next recognition clears it
   * rather than the end of this one, so a result a dying recognition delivers late still finds the
   * utterance it belongs to.
   */
  private utterances = new Map<number, Utterance>();
  /** The hints the next recognition starts with; the latest list the page handed over. */
  private hints: readonly string[];
  /**
   * False once the browser's service said `phrases-not-supported`: the recognitions after it start
   * without phrases, since every one would fail the same way and cost the student a restart.
   */
  private biasing = true;

  constructor(callbacks: TranscriberCallbacks, options: WebSpeechTranscriberOptions = {}) {
    this.provider = WEB_SPEECH_PROVIDER;
    this.language = options.language ?? WEB_SPEECH_LANGUAGE;
    this.callbacks = callbacks;
    this.hints = [...(options.vocabularyHints ?? [])];
  }

  /**
   * Replaces the hints. The running recognition keeps the ones it started with -- the Web Speech
   * API reads `phrases` at `start()` -- and the next one, which Chrome starts after every stretch
   * of silence, uses these.
   */
  setVocabularyHints(hints: readonly string[]): void {
    this.hints = [...hints];
  }

  /**
   * Starts listening. Calling it again while it listens does nothing, so the page cannot end up
   * with two recognitions competing for the microphone.
   */
  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    const problem = this.begin();
    if (problem === null) return;
    this.giveUp(problem);
    throw new TranscriberError(problem);
  }

  /** Ends the session's transcription: the recognition is aborted and nothing restarts. */
  stop(): void {
    this.abandon();
  }

  /** Starts one recognition; null once it listens, the problem when the browser refused. */
  private begin(): TranscriberProblem | null {
    const recognition = this.construct();
    if (recognition === null) {
      return {
        code: "unsupported",
        detail: "the browser offers neither SpeechRecognition nor webkitSpeechRecognition",
        recoverable: false,
      };
    }
    recognition.lang = this.language;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    this.applyHints(recognition);
    recognition.onresult = (event) => this.onResult(event);
    recognition.onerror = (event) => this.onError(event);
    recognition.onend = () => this.onEnd();
    this.recognition = recognition;
    this.utterances.clear();
    try {
      recognition.start();
      return null;
    } catch (problem) {
      // A browser throws `InvalidStateError` here when it will not listen at all; the student gets
      // the reason instead of a page that looks like it is transcribing and never does.
      this.recognition = null;
      return {
        code: "unavailable",
        detail: problem instanceof Error ? `${problem.name}: ${problem.message}` : String(problem),
        recoverable: false,
      };
    }
  }

  /**
   * Puts the hints on a recognition that is about to start, where the browser can bias at all. A
   * browser that has the API but refuses a phrase (it throws) recognizes without hints instead of
   * failing the session over them.
   */
  private applyHints(recognition: SpeechRecognitionLike): void {
    if (!this.biasing || this.hints.length === 0 || !("phrases" in recognition)) return;
    const Phrase = speechRecognitionPhraseConstructor();
    if (Phrase === null) return;
    try {
      recognition.phrases = this.hints.map((hint) => new Phrase(hint, VOCABULARY_HINT_BOOST));
    } catch {
      this.biasing = false;
    }
  }

  private construct(): SpeechRecognitionLike | null {
    const Recognition = speechRecognitionConstructor();
    return Recognition === null ? null : new Recognition();
  }

  private onResult(event: SpeechResultEvent): void {
    if (!this.running) return;
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      this.emitResult(event.results[index], index);
    }
  }

  /** One result of one event: an interim or the final of the utterance its index stands for. */
  private emitResult(result: SpeechResult | undefined, index: number): void {
    if (result === undefined) return;
    const alternative: SpeechAlternative | undefined = result[0];
    const text = (alternative?.transcript ?? "").trim();
    if (text === "") {
      // Nothing to send, and a settled result will not grow text either.
      if (result.isFinal) this.utterances.delete(index);
      return;
    }
    const utterance = this.utteranceAt(index);
    if (result.isFinal) this.utterances.delete(index);
    const segment: ClientSegment = {
      segment_id: utterance.segmentId,
      client_start_ms: utterance.startMs,
      // `Date.now()` steps backwards when the clock is corrected, and the wire decoder refuses a
      // segment that ends before it starts.
      client_end_ms: Math.max(utterance.startMs, Date.now()),
      text,
      provider: this.provider,
      language: this.language,
    };
    const kind: TranscriptKind = result.isFinal ? "final" : "partial";
    // The protocol's `confidence` is the recognizer's own score, and an interim has none, so a
    // missing or zero one stays absent instead of claiming a certainty nobody reported.
    if (alternative !== undefined && alternative.confidence > 0) {
      segment.confidence = alternative.confidence;
    }
    this.callbacks.onSegment(segment, kind);
  }

  private utteranceAt(index: number): Utterance {
    const known = this.utterances.get(index);
    if (known !== undefined) return known;
    const utterance: Utterance = { segmentId: newSegmentId(), startMs: Date.now() };
    this.utterances.set(index, utterance);
    return utterance;
  }

  private onError(event: SpeechErrorEvent): void {
    if (event.error === PHRASES_NOT_SUPPORTED) {
      // The `end` that follows restarts the recognition, this time without phrases.
      this.biasing = false;
      return;
    }
    const problem = speechProblem(event.error, event.message);
    if (problem === null) return;
    if (problem.recoverable) {
      // The `end` a browser reports right after the error is what restarts it; the page shows the
      // student what is wrong in the meantime.
      this.report(problem);
      return;
    }
    this.giveUp(problem);
  }

  private onEnd(): void {
    this.recognition = null;
    if (!this.running) return;
    this.cancelRestart();
    this.restart = setTimeout(() => {
      this.restart = null;
      if (!this.running) return;
      const problem = this.begin();
      if (problem !== null) this.giveUp(problem);
    }, RESTART_DELAY_MS);
  }

  /** Ends transcription for good and tells the page why. */
  private giveUp(problem: TranscriberProblem): void {
    this.abandon();
    this.report(problem);
  }

  private abandon(): void {
    this.running = false;
    this.cancelRestart();
    const recognition = this.recognition;
    this.recognition = null;
    this.utterances.clear();
    // `abort()` and not `stop()`: a session that is over takes no last result, and the `end` an
    // aborted recognition reports finds `running` false, so it starts nothing.
    recognition?.abort();
  }

  private cancelRestart(): void {
    if (this.restart === null) return;
    clearTimeout(this.restart);
    this.restart = null;
  }

  private report(problem: TranscriberProblem): void {
    this.callbacks.onProblem?.(problem);
  }
}
