/**
 * The Web Speech transcriber against the fake `SpeechRecognition` (#40). The fake is driven the way
 * a browser behaves -- `result` events with a `resultIndex`, an `end` after a stretch of silence,
 * an `error` followed by that `end` -- and every segment the transcriber emits is decoded by the
 * bindings' own `parseClientEvent`, so what goes to the socket is contract-valid instead of merely
 * matching what this test invented.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type ClientEvent, parseClientEvent } from "../protocol";
import { type ClientSegment, type TranscriptKind, WEB_SPEECH_PROVIDER } from "./sessionSocket";
import {
  type FakeSpeechRecognition,
  installSpeechRecognitionFake,
  type SpeechGlobalName,
  swapGlobal,
} from "./testing";
import {
  type ClientTranscriber,
  TranscriberError,
  type TranscriberProblem,
} from "./transcriber";
import {
  RESTART_DELAY_MS,
  WEB_SPEECH_LANGUAGE,
  WebSpeechTranscriber,
  webSpeechSupported,
} from "./webSpeechTranscriber";

/** The client clock a session starts at, of the same era as `protocol/examples/`. */
const START_MS = 1790251200000;

interface Emitted {
  readonly segment: ClientSegment;
  readonly kind: TranscriptKind;
}

interface Harness {
  /** Typed as the interface, which is the page's whole view of a transcriber. */
  readonly transcriber: ClientTranscriber;
  /** Every recognition the transcriber constructed, in order: one per generation. */
  readonly recognitions: readonly FakeSpeechRecognition[];
  readonly segments: Emitted[];
  readonly problems: TranscriberProblem[];
}

const restores: Array<() => void> = [];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
  vi.setSystemTime(START_MS);
});

afterEach(() => {
  vi.useRealTimers();
  while (restores.length > 0) restores.pop()?.();
});

/** The transcriber on the fake Web Speech API, recording every segment and every problem. */
function session(
  globals: SpeechGlobalName[] = ["SpeechRecognition", "webkitSpeechRecognition"],
): Harness {
  const installed = installSpeechRecognitionFake(globals);
  restores.push(installed.restore);
  const segments: Emitted[] = [];
  const problems: TranscriberProblem[] = [];
  const transcriber = new WebSpeechTranscriber({
    onSegment: (segment, kind) => segments.push({ segment, kind }),
    onProblem: (problem) => problems.push(problem),
  });
  return { transcriber, recognitions: installed.recognitions, segments, problems };
}

/** The recognition that is running now, which is the last one constructed. */
function current(harness: Harness): FakeSpeechRecognition {
  const recognition = harness.recognitions.at(-1);
  if (recognition === undefined) throw new Error("the transcriber constructed no recognition");
  return recognition;
}

/** A browser ending the recognition, and the transcriber starting the next one after its pause. */
async function endAndRestart(harness: Harness): Promise<FakeSpeechRecognition> {
  current(harness).emitEnd();
  await vi.advanceTimersByTimeAsync(RESTART_DELAY_MS);
  return current(harness);
}

/** One segment the transcriber emitted, decoded by the bindings: proof it is the frame it claims. */
function frame(harness: Harness, index = 0): ClientEvent {
  const emitted = harness.segments[index];
  if (emitted === undefined) throw new Error(`the transcriber emitted no segment ${index}`);
  return parseClientEvent({
    ...emitted.segment,
    type: `transcript.client.${emitted.kind}`,
  });
}

/** What `start()` rejected with, or null when it did not. */
async function refused(harness: Harness): Promise<unknown> {
  return harness.transcriber.start().then(
    () => null,
    (problem: unknown) => problem,
  );
}

describe("webSpeechSupported", () => {
  it("finds the API in either of the two globals a browser offers it in", () => {
    restores.push(installSpeechRecognitionFake().restore);
    expect(webSpeechSupported()).toBe(true);
  });

  it("finds the API of a Chrome-style browser that only has the prefixed global", () => {
    restores.push(installSpeechRecognitionFake(["webkitSpeechRecognition"]).restore);
    expect(webSpeechSupported()).toBe(true);
  });

  it("reports a browser without the API at all", () => {
    restores.push(installSpeechRecognitionFake([]).restore);
    expect(webSpeechSupported()).toBe(false);
  });
});

describe("start", () => {
  it("asks for continuous Spanish recognition with interim results and starts it", async () => {
    const harness = session();
    await harness.transcriber.start();

    const recognition = current(harness);
    expect(recognition.lang).toBe(WEB_SPEECH_LANGUAGE);
    expect(recognition.continuous).toBe(true);
    expect(recognition.interimResults).toBe(true);
    expect(recognition.maxAlternatives).toBe(1);
    expect(recognition.startCount).toBe(1);
    expect(recognition.abortCount).toBe(0);
    expect(harness.transcriber.provider).toBe(WEB_SPEECH_PROVIDER);
  });

  it("listens with the prefixed global of a Chrome-style browser", async () => {
    const harness = session(["webkitSpeechRecognition"]);
    await harness.transcriber.start();
    current(harness).emitResult([{ transcript: "la derivada", final: true }]);

    expect(harness.segments).toHaveLength(1);
    expect(harness.segments[0]?.segment.text).toBe("la derivada");
  });

  it("constructs no second recognition when it is started twice", async () => {
    const harness = session();
    await harness.transcriber.start();
    await harness.transcriber.start();

    expect(harness.recognitions).toHaveLength(1);
    expect(current(harness).startCount).toBe(1);
  });

  it("refuses a browser without the API, in Spanish the page can pick, and constructs nothing", async () => {
    const harness = session([]);
    const thrown = await refused(harness);

    expect(thrown).toBeInstanceOf(TranscriberError);
    expect((thrown as TranscriberError).code).toBe("unsupported");
    expect(harness.problems).toEqual([
      {
        code: "unsupported",
        detail: expect.stringContaining("SpeechRecognition"),
        recoverable: false,
      },
    ]);
    expect(harness.recognitions).toHaveLength(0);
  });

  it("refuses a browser whose recognition will not start, with the reason it gave", async () => {
    const harness = session();
    restores.push(swapGlobal("SpeechRecognition", RefusingRecognition));

    const thrown = await refused(harness);

    expect(thrown).toBeInstanceOf(TranscriberError);
    expect((thrown as TranscriberError).code).toBe("unavailable");
    expect(harness.problems).toEqual([
      {
        code: "unavailable",
        detail: "InvalidStateError: recognition already started",
        recoverable: false,
      },
    ]);
    expect(harness.segments).toHaveLength(0);
  });
});

/**
 * A browser that constructs a recognition and then refuses to listen, the way Chrome does when it
 * is already recognizing: `start()` throws instead of reporting an `error` event.
 */
class RefusingRecognition {
  lang = "";
  continuous = false;
  interimResults = false;
  maxAlternatives = 1;
  onresult: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onend: ((event: Event) => void) | null = null;

  start(): void {
    const problem = new Error("recognition already started");
    problem.name = "InvalidStateError";
    throw problem;
  }

  abort(): void {}
}

describe("segments", () => {
  it("emits the interim partials of an utterance and the final that settles them as one segment", async () => {
    const harness = session();
    await harness.transcriber.start();
    const recognition = current(harness);

    vi.setSystemTime(START_MS + 1200);
    recognition.emitResult([{ transcript: " la derivada de una" }]);
    vi.setSystemTime(START_MS + 1800);
    recognition.emitResult([{ transcript: "la derivada de una constante" }]);
    vi.setSystemTime(START_MS + 2500);
    recognition.emitResult([
      { transcript: "la derivada de una constante es cero", final: true, confidence: 0.92 },
    ]);

    expect(harness.segments.map((emitted) => emitted.kind)).toEqual([
      "partial",
      "partial",
      "final",
    ]);
    const [first, second, final] = harness.segments.map((emitted) => emitted.segment);
    expect(second?.segment_id).toBe(first?.segment_id);
    expect(final?.segment_id).toBe(first?.segment_id);
    expect(final).toEqual({
      segment_id: expect.any(String),
      // The utterance started when the recognizer first reported it, and ended when it settled.
      client_start_ms: START_MS + 1200,
      client_end_ms: START_MS + 2500,
      text: "la derivada de una constante es cero",
      provider: WEB_SPEECH_PROVIDER,
      language: WEB_SPEECH_LANGUAGE,
      confidence: 0.92,
    });
  });

  it("mints an id of the shape the protocol accepts, and one per utterance", async () => {
    const harness = session();
    await harness.transcriber.start();
    const recognition = current(harness);

    recognition.emitResult([{ transcript: "primero" }]);
    recognition.emitResult([{ transcript: "primero" }, { transcript: "segundo" }], 1);
    recognition.emitResult([{ transcript: "primero dicho", final: true }]);

    const ids = harness.segments.map((emitted) => emitted.segment.segment_id);
    expect(ids).toHaveLength(3);
    expect(ids[0]).toMatch(/^seg-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
    expect(ids[2]).toBe(ids[0]);
    expect(ids[1]).not.toBe(ids[0]);
  });

  it("reads an event from its resultIndex, leaving the utterances before it alone", async () => {
    const harness = session();
    await harness.transcriber.start();

    current(harness).emitResult([{ transcript: "ya dicho" }, { transcript: "y lo nuevo" }], 1);

    expect(harness.segments).toHaveLength(1);
    expect(harness.segments[0]?.segment.text).toBe("y lo nuevo");
  });

  it("sends every segment as a frame the bindings accept", async () => {
    const harness = session();
    await harness.transcriber.start();
    const recognition = current(harness);

    recognition.emitResult([{ transcript: "una integral" }]);
    recognition.emitResult([{ transcript: "una integral definida", final: true, confidence: 0.8 }]);

    expect(frame(harness, 0).type).toBe("transcript.client.partial");
    expect(frame(harness, 1).type).toBe("transcript.client.final");
  });

  it("reports no confidence for an interim result, which a browser does not score", async () => {
    const harness = session();
    await harness.transcriber.start();

    current(harness).emitResult([{ transcript: "una integral" }]);

    const partial = harness.segments[0]?.segment;
    expect(partial).toBeDefined();
    expect(partial).not.toHaveProperty("confidence");
  });

  it("drops a result with no text in it", async () => {
    const harness = session();
    await harness.transcriber.start();
    const recognition = current(harness);

    recognition.emitResult([{ transcript: "   " }]);
    recognition.emitResult([{ transcript: "", final: true }]);

    expect(harness.segments).toHaveLength(0);
  });

  it("keeps an utterance's start when the clock steps backwards under it", async () => {
    const harness = session();
    await harness.transcriber.start();
    const recognition = current(harness);

    vi.setSystemTime(START_MS + 5000);
    recognition.emitResult([{ transcript: "antes del salto" }]);
    vi.setSystemTime(START_MS + 1000);
    recognition.emitResult([{ transcript: "antes del salto", final: true }]);

    const final = harness.segments[1]?.segment;
    expect(final?.client_start_ms).toBe(START_MS + 5000);
    expect(final?.client_end_ms).toBe(START_MS + 5000);
    expect(() => frame(harness, 1)).not.toThrow();
  });
});

describe("restart", () => {
  it("starts a fresh recognition when a browser ends one while the session runs", async () => {
    const harness = session();
    await harness.transcriber.start();
    const first = current(harness);
    first.emitResult([{ transcript: "hola", final: true }]);

    first.emitEnd();
    expect(harness.recognitions).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(RESTART_DELAY_MS - 1);
    expect(harness.recognitions).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);

    const second = current(harness);
    expect(harness.recognitions).toHaveLength(2);
    expect(second).not.toBe(first);
    expect(second.startCount).toBe(1);
    expect(second.lang).toBe(WEB_SPEECH_LANGUAGE);
    expect(second.continuous).toBe(true);
    second.emitResult([{ transcript: "sigue aquí", final: true }]);
    expect(harness.segments.map((emitted) => emitted.segment.text)).toEqual([
      "hola",
      "sigue aquí",
    ]);
  });

  it("counts a new recognition's utterances from scratch and drops the interim the old one left", async () => {
    const harness = session();
    await harness.transcriber.start();
    current(harness).emitResult([{ transcript: "se queda a medias" }]);
    const pending = harness.segments[0]?.segment.segment_id;

    const second = await endAndRestart(harness);
    second.emitResult([{ transcript: "de la segunda escucha" }]);

    // The interim of the ended recognition is never settled into a final, and the new one's index
    // 0 is another utterance, so it cannot inherit that id.
    expect(harness.segments).toHaveLength(2);
    expect(harness.segments[1]?.segment.segment_id).not.toBe(pending);
  });

  it("keeps listening through the ends of a session in which the student says nothing", async () => {
    const harness = session();
    await harness.transcriber.start();

    await endAndRestart(harness);
    await endAndRestart(harness);
    await endAndRestart(harness);

    expect(harness.recognitions).toHaveLength(4);
    expect(harness.problems).toEqual([]);
  });

  it("reports a failure of the recognizer's own service and carries on listening", async () => {
    const harness = session();
    await harness.transcriber.start();
    const recognition = current(harness);

    recognition.emitError("network", "the speech service is unreachable");
    expect(harness.problems).toEqual([
      {
        code: "network",
        detail: "network: the speech service is unreachable",
        recoverable: true,
      },
    ]);

    const second = await endAndRestart(harness);
    expect(second).not.toBe(recognition);
    second.emitResult([{ transcript: "otra vez", final: true }]);
    expect(harness.segments).toHaveLength(1);
  });

  it("says nothing of the silence a continuous recognition reports, and carries on", async () => {
    const harness = session();
    await harness.transcriber.start();

    current(harness).emitError("no-speech");
    const second = await endAndRestart(harness);

    expect(harness.problems).toEqual([]);
    expect(second.startCount).toBe(1);
  });

  it("retries a code it does not know instead of ending the session over it", async () => {
    const harness = session();
    await harness.transcriber.start();

    current(harness).emitError("bad-grammar");
    await endAndRestart(harness);

    expect(harness.problems).toEqual([
      { code: "unavailable", detail: "bad-grammar", recoverable: true },
    ]);
    expect(harness.recognitions).toHaveLength(2);
  });
});

describe("failures it cannot go on through", () => {
  it.each([
    ["not-allowed", "permission-denied"],
    ["service-not-allowed", "permission-denied"],
    ["audio-capture", "unavailable"],
    ["language-not-supported", "unavailable"],
  ])("stops on %s, which the page explains as %s", async (error, code) => {
    const harness = session();
    await harness.transcriber.start();
    const recognition = current(harness);

    recognition.emitError(error);

    expect(harness.problems).toEqual([
      { code, detail: error, recoverable: false },
    ]);
    expect(recognition.abortCount).toBe(1);
    // A browser reports `end` right after the error; a stopped session starts nothing from it.
    recognition.emitEnd();
    await vi.advanceTimersByTimeAsync(RESTART_DELAY_MS);
    expect(harness.recognitions).toHaveLength(1);
    recognition.emitResult([{ transcript: "nada de esto llega", final: true }]);
    expect(harness.segments).toHaveLength(0);
  });
});

describe("stop", () => {
  it("aborts the recognition, emits nothing after it and starts no new one", async () => {
    const harness = session();
    await harness.transcriber.start();
    const recognition = current(harness);
    recognition.emitResult([{ transcript: "hasta aquí" }]);

    harness.transcriber.stop();

    expect(recognition.abortCount).toBe(1);
    expect(recognition.stopCount).toBe(0);
    recognition.emitResult([{ transcript: "y esto ya no", final: true }]);
    recognition.emitEnd();
    await vi.advanceTimersByTimeAsync(RESTART_DELAY_MS);
    expect(harness.segments).toHaveLength(1);
    expect(harness.recognitions).toHaveLength(1);
  });

  it("cancels a restart that was already on its way", async () => {
    const harness = session();
    await harness.transcriber.start();
    const recognition = current(harness);

    recognition.emitEnd();
    harness.transcriber.stop();
    await vi.advanceTimersByTimeAsync(RESTART_DELAY_MS);

    expect(harness.recognitions).toHaveLength(1);
  });

  it("starts listening again when the page asks for it after a stop", async () => {
    const harness = session();
    await harness.transcriber.start();
    harness.transcriber.stop();

    await harness.transcriber.start();
    current(harness).emitResult([{ transcript: "otra sesión", final: true }]);

    expect(harness.recognitions).toHaveLength(2);
    expect(harness.segments).toHaveLength(1);
  });
});
