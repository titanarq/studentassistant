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
): SpeechFakes {
  activeRecognitions = [];
  const restores = globals.map((name) => swapGlobal(name, FakeSpeechRecognition));
  return {
    recognitions: activeRecognitions,
    restore: () => {
      for (const restore of restores.reverse()) restore();
    },
  };
}
