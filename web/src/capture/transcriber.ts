/**
 * The seam between the capture page and whichever recognizer a session runs (#40, ADR-0008).
 * `hello.ack.stt_mode` picks the implementation: in `client` mode the browser recognizes the
 * speech itself (`webSpeechTranscriber.ts`) and in `server` mode the microphone audio goes to the
 * backend's provider (`audioStreamTranscriber.ts`), which calls `onSegment` never because the
 * backend sends the transcript back over the socket. The page talks to this interface and to
 * nothing else, so a provider is swapped without touching it.
 */

import type { ClientSegment, TranscriptKind } from "./sessionSocket";

/**
 * Why a transcriber cannot go on, in the codes the page owns the Spanish for: `unsupported` (this
 * browser has no such recognizer), `permission-denied` (the student refused the microphone, or the
 * browser will not give it), `network` (the recognizer's own service is unreachable) and
 * `unavailable` (anything else: no audio device, a language the recognizer does not know).
 */
export type TranscriberProblemCode =
  | "unsupported"
  | "permission-denied"
  | "network"
  | "unavailable";

/** One failure of a transcriber, which the page turns into a Spanish message for the student. */
export interface TranscriberProblem {
  readonly code: TranscriberProblemCode;
  /** The recognizer's own code and wording, for the log; never shown to the student. */
  readonly detail: string;
  /** True while the transcriber keeps trying by itself, false once it has stopped for good. */
  readonly recoverable: boolean;
}

/**
 * A `start()` that never got going. It carries the problem it was reported with, so the page runs
 * the refusal of a transcriber and a failure mid-session through the same Spanish message.
 */
export class TranscriberError extends Error implements TranscriberProblem {
  readonly code: TranscriberProblemCode;
  readonly detail: string;
  readonly recoverable = false;

  constructor(problem: TranscriberProblem) {
    super(`${problem.code}: ${problem.detail}`);
    this.name = "TranscriberError";
    this.code = problem.code;
    this.detail = problem.detail;
  }
}

/** What the page hands a transcriber: where its utterances go and what it says when it fails. */
export interface TranscriberCallbacks {
  /**
   * One utterance of the recognizer, as the `transcript.client.partial` or
   * `transcript.client.final` frame it goes out as. The partials and the final of an utterance
   * share its `segment_id`, which is what lets the page replace one with the other.
   */
  onSegment(segment: ClientSegment, kind: TranscriptKind): void;
  /** A failure once the transcriber was running; `recoverable` says whether it still listens. */
  onProblem?(problem: TranscriberProblem): void;
}

/** One session's recognizer, whichever provider it is (ADR-0008). */
export interface ClientTranscriber {
  /** The `provider` its segments carry, which is the `stt_provider` `hello` announced. */
  readonly provider: string;
  /**
   * Begins transcribing and resolves once it is listening. It rejects with a `TranscriberError`
   * when it cannot begin at all, which the same problem was reported through `onProblem` for.
   */
  start(): Promise<void>;
  /** Ends it for good: no restart and no further segment. */
  stop(): void;
  /**
   * Since protocol 1.4 (#227): the session's vocabulary hints, most important first, which replace
   * the previous list. A recognizer that takes phrase hints biases the recognitions it starts from
   * then on towards them; one that does not ignores them. Optional, because a transcriber whose
   * audio goes to the backend has nothing to bias: the backend applies the hints itself.
   */
  setVocabularyHints?(hints: readonly string[]): void;
}
