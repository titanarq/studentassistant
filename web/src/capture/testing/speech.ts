/**
 * A fake Web Speech `SpeechRecognition` (#40): the code under test constructs it through the
 * global it looks up, configures `lang`/`continuous`/`interimResults`, and the test drives what the
 * browser would say back with `emitResult`, `emitEnd` and `emitError`. No audio is involved.
 */

import { FakeEventTarget, swapGlobal } from "./support";

/** One alternative of a result; the capture code reads `transcript`. */
export interface FakeSpeechAlternative {
  readonly transcript: string;
  readonly confidence: number;
}

/** One `SpeechRecognitionResult`: array-like over its alternatives, plus `isFinal`. */
export interface FakeSpeechResult extends ArrayLike<FakeSpeechAlternative> {
  readonly isFinal: boolean;
  item(index: number): FakeSpeechAlternative;
}

/** A `SpeechRecognitionResultList`: array-like over the results one event carries. */
export interface FakeSpeechResultList extends ArrayLike<FakeSpeechResult> {
  item(index: number): FakeSpeechResult;
}

/** A `result` event. */
export interface FakeSpeechResultEvent extends Event {
  readonly resultIndex: number;
  readonly results: FakeSpeechResultList;
}

/** An `error` event; `error` is the Web Speech code ("no-speech", "not-allowed", "network", ...). */
export interface FakeSpeechErrorEvent extends Event {
  readonly error: string;
  readonly message: string;
}

/** What a test hands to `emitResult`: one result per utterance, interim unless `final` says so. */
export interface FakeUtterance {
  readonly transcript: string;
  readonly final?: boolean;
  readonly confidence?: number;
}

let activeRecognitions: FakeSpeechRecognition[] = [];

export class FakeSpeechRecognition extends FakeEventTarget {
  /** What the code under test configured, for a test to assert against. */
  lang = "";
  continuous = false;
  interimResults = false;
  maxAlternatives = 1;
  startCount = 0;
  stopCount = 0;
  abortCount = 0;
  onstart: ((event: Event) => void) | null = null;
  onresult: ((event: FakeSpeechResultEvent) => void) | null = null;
  onerror: ((event: FakeSpeechErrorEvent) => void) | null = null;
  onend: ((event: Event) => void) | null = null;

  constructor() {
    super();
    activeRecognitions.push(this);
  }

  start(): void {
    this.startCount += 1;
  }

  stop(): void {
    this.stopCount += 1;
  }

  abort(): void {
    this.abortCount += 1;
  }

  /** Test control: the browser started listening. */
  emitStart(): void {
    this.emit("start");
  }

  /** Test control: the browser recognised these utterances, from `resultIndex` on. */
  emitResult(utterances: FakeUtterance[], resultIndex = 0): void {
    this.emit("result", { resultIndex, results: buildResultList(utterances) });
  }

  /** Test control: the browser stopped listening, which a continuous recognition must survive. */
  emitEnd(): void {
    this.emit("end");
  }

  /** Test control: the browser failed, with a Web Speech error code. */
  emitError(error: string, message = ""): void {
    this.emit("error", { error, message });
  }
}

/**
 * A fake `SpeechRecognitionPhrase` (#227): the contextual-biasing term of the Web Speech API, one
 * phrase and its boost. The real constructor throws `SyntaxError` for a boost outside [0, 10], and
 * so does this one, so a transcriber that picked a bad boost fails a test instead of a browser.
 */
export class FakeSpeechRecognitionPhrase {
  readonly phrase: string;
  readonly boost: number;

  constructor(phrase: string, boost = 1.0) {
    if (!(boost >= 0 && boost <= 10)) {
      throw new DOMException(`boost ${boost} is outside [0, 10]`, "SyntaxError");
    }
    this.phrase = phrase;
    this.boost = boost;
  }
}

/**
 * A recognition of a browser with contextual biasing (#227): the `phrases` list a transcriber
 * assigns before `start()`. Whether the browser's service then honours them is the test's to say,
 * with `emitError("phrases-not-supported")`, which is what Chrome answers for a cloud recognition.
 */
export class FakeBiasingSpeechRecognition extends FakeSpeechRecognition {
  phrases: FakeSpeechRecognitionPhrase[] = [];
  /** The `phrases` each `start()` found, in order, for a test to assert what every restart used. */
  readonly phrasesAtStart: string[][] = [];

  override start(): void {
    super.start();
    this.phrasesAtStart.push(this.phrases.map((entry) => entry.phrase));
  }
}

function buildResultList(utterances: FakeUtterance[]): FakeSpeechResultList {
  return asArrayLike(utterances.map(buildResult)) as FakeSpeechResultList;
}

function buildResult(utterance: FakeUtterance): FakeSpeechResult {
  const isFinal = utterance.final ?? false;
  const alternative: FakeSpeechAlternative = {
    transcript: utterance.transcript,
    // Chrome reports no confidence for an interim result, so neither does the fake.
    confidence: utterance.confidence ?? (isFinal ? 0.9 : 0),
  };
  const result = asArrayLike([alternative]) as unknown as Record<string, unknown>;
  result.isFinal = isFinal;
  return result as unknown as FakeSpeechResult;
}

/** The array-like shape the Web Speech API uses: `length`, numeric keys and `item()`. */
function asArrayLike<T>(items: T[]): ArrayLike<T> & { item(index: number): T } {
  const list: Record<string, unknown> = {
    length: items.length,
    item: (index: number) => items[index],
  };
  items.forEach((item, index) => {
    list[index] = item;
  });
  return list as unknown as ArrayLike<T> & { item(index: number): T };
}

/** The global slots a browser may offer the Web Speech API in. */
export type SpeechGlobalName = "SpeechRecognition" | "webkitSpeechRecognition";

export interface SpeechFakeOptions {
  /**
   * True for a browser with contextual biasing: the recognition has a `phrases` list and the
   * `SpeechRecognitionPhrase` global exists. False by default, a browser without phrase hints.
   */
  phrases?: boolean;
}

export interface SpeechFakes {
  /** Every recognition the code under test constructed, in order. */
  readonly recognitions: FakeSpeechRecognition[];
  restore(): void;
}

/**
 * Puts the fake on the given globals, both by default; name only `webkitSpeechRecognition` for a
 * Chrome-style browser and neither for one without the API at all.
 */
export function installSpeechRecognitionFake(
  globals: SpeechGlobalName[] = ["SpeechRecognition", "webkitSpeechRecognition"],
  options: SpeechFakeOptions = {},
): SpeechFakes {
  activeRecognitions = [];
  const phrases = options.phrases ?? false;
  const Recognition = phrases ? FakeBiasingSpeechRecognition : FakeSpeechRecognition;
  const restores = globals.map((name) => swapGlobal(name, Recognition));
  if (phrases) restores.push(swapGlobal("SpeechRecognitionPhrase", FakeSpeechRecognitionPhrase));
  return {
    recognitions: activeRecognitions,
    restore: () => {
      for (const restore of restores.reverse()) restore();
    },
  };
}
