/**
 * The server-side STT path as a `ClientTranscriber` (#40, ADR-0008). When `hello.ack` answers
 * `stt_mode: "server"` the browser recognizes nothing -- no Web Speech recognition runs at all --
 * and this streams the microphone to the backend's provider instead: an AudioWorklet captures the
 * audio, `audioFrames.ts` resamples it to PCM16 16 kHz mono and puts it in the frame layout
 * protocol/README.md defines, and the session socket sends one binary message per chunk.
 *
 * It therefore emits no segment: the transcript comes back over the socket as the backend's own
 * normalised `transcript.partial` / `transcript.final`, which is what makes every client show the
 * same words. What it does report is why the microphone would not run, in the same codes the Web
 * Speech transcriber uses, so the page shows one set of Spanish messages for either mode.
 */

import type { AudioFormat } from "../protocol";
import { AudioResampler, encodeAudioFrame, pcm16Bytes } from "./audioFrames";
import {
  PCM_WORKLET_CHUNK_MS,
  PCM_WORKLET_PROCESSOR,
  type PcmWorkletChunk,
  type PcmWorkletOptions,
} from "./pcmWorklet";
// The processor's own module, as the URL `addModule()` loads: Vite compiles this file into a worker
// chunk and hands over its address, which is the only way a TypeScript module reaches a worklet.
import pcmWorkletUrl from "./pcmWorklet.ts?worker&url";
import { CLIENT_AUDIO_FORMAT } from "./sessionSocket";
import {
  type ClientTranscriber,
  TranscriberError,
  type TranscriberCallbacks,
  type TranscriberProblem,
  type TranscriberProblemCode,
} from "./transcriber";

/** What this transcriber calls itself; it emits no segment, so no frame ever carries it. */
export const AUDIO_STREAM_PROVIDER = "audio-stream";

/** The two slots a browser offers an `AudioContext` in; Safari has only the prefixed one. */
const AUDIO_CONTEXT_GLOBALS = ["AudioContext", "webkitAudioContext"] as const;

/**
 * What the microphone is asked for: voice, and no video, which the camera's own stream owns. The
 * three processing flags are the browser's, and they are what makes a laptop's built-in microphone
 * usable for speech at all.
 */
const MICROPHONE_CONSTRAINTS: MediaStreamConstraints = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  video: false,
};

/** What a `getUserMedia` refusal means here, by the DOM exception name a browser reports it with. */
const MICROPHONE_REFUSALS: Record<string, TranscriberProblemCode> = {
  NotAllowedError: "permission-denied",
  SecurityError: "permission-denied",
  NotFoundError: "unavailable",
  NotReadableError: "unavailable",
  OverconstrainedError: "unavailable",
};

type AudioContextConstructor = typeof AudioContext;

/** Where the encoded frames go; the session socket answers with its own `sendAudio`. */
export interface AudioFrameSink {
  sendAudio(frame: Uint8Array<ArrayBuffer>): void;
}

export interface AudioStreamTranscriberOptions {
  /** The session's socket, or anything else that puts a frame on the wire. */
  sink: AudioFrameSink;
  /** The audio `hello.ack` asked for; the format this client announces by default. */
  audioFormat?: AudioFormat;
  /** The processor module to load; the URL Vite builds from `pcmWorklet.ts` by default. */
  workletUrl?: string;
  /** How much audio one frame carries, in ms; a tenth of a second by default. */
  chunkMs?: number;
}

/** A global slot, in the one place this file reads a browser API from. */
function globalSlot(name: string): unknown {
  const value = (globalThis as Record<string, unknown>)[name];
  if (value !== undefined) return value;
  const scope = (globalThis as unknown as { window?: Record<string, unknown> }).window;
  return scope === undefined ? undefined : scope[name];
}

function audioContextConstructor(): AudioContextConstructor | null {
  for (const name of AUDIO_CONTEXT_GLOBALS) {
    const value = globalSlot(name);
    if (typeof value === "function") return value as AudioContextConstructor;
  }
  return null;
}

/**
 * Whether this browser can stream audio at all. The page asks before it starts a session, because
 * `hello.capabilities.audio_format` is announced only when the client can stream it, and a backend
 * that then answers `stt_mode: "server"` would otherwise get a client that cannot obey.
 */
export function audioStreamSupported(): boolean {
  return (
    audioContextConstructor() !== null &&
    typeof globalSlot("AudioWorkletNode") === "function" &&
    typeof navigator.mediaDevices?.getUserMedia === "function"
  );
}

function microphoneProblem(problem: unknown): TranscriberProblem {
  const name = problem instanceof DOMException ? problem.name : "";
  return {
    code: MICROPHONE_REFUSALS[name] ?? "unavailable",
    detail: describe(problem),
    recoverable: false,
  };
}

function describe(problem: unknown): string {
  return problem instanceof Error ? `${problem.name}: ${problem.message}` : String(problem);
}

export class AudioStreamTranscriber implements ClientTranscriber {
  readonly provider = AUDIO_STREAM_PROVIDER;

  private readonly callbacks: TranscriberCallbacks;
  private readonly sink: AudioFrameSink;
  /** The rate a frame's payload carries, which is what `hello.ack` asked for. */
  private readonly targetRate: number;
  private readonly workletUrl: string;
  private readonly chunkMs: number;

  /** True from `start()` until `stop()` or a fatal problem; the only thing that keeps frames going. */
  private running = false;
  private context: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private stream: MediaStream | null = null;
  private resampler: AudioResampler | null = null;
  /** The per-session frame counter; every frame carries the next one, from 0. */
  private seq = 0;
  /** The client clock reading the audio clock started from; see `clientTimeMs`. */
  private clockOriginMs = 0;

  constructor(callbacks: TranscriberCallbacks, options: AudioStreamTranscriberOptions) {
    const format = options.audioFormat ?? CLIENT_AUDIO_FORMAT;
    this.callbacks = callbacks;
    this.sink = options.sink;
    this.targetRate = format.sample_rate_hz;
    this.workletUrl = options.workletUrl ?? pcmWorkletUrl;
    this.chunkMs = options.chunkMs ?? PCM_WORKLET_CHUNK_MS;
  }

  /**
   * Takes the microphone, loads the processor and starts streaming; resolves once the graph runs.
   * Calling it again while it streams does nothing, so the page cannot end up with two graphs
   * competing for the same microphone.
   */
  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    const problem = await this.begin();
    if (problem === null) return;
    this.giveUp(problem);
    throw new TranscriberError(problem);
  }

  /**
   * Ends the streaming for good: the graph comes apart, the microphone is released and no further
   * frame goes out. Up to one chunk of audio is still inside the processor and is dropped with it,
   * which is what the Web Speech transcriber does with an interim it never settled.
   */
  stop(): void {
    this.abandon();
  }

  /** Builds the graph; null once it runs, the problem when the browser refused any part of it. */
  private async begin(): Promise<TranscriberProblem | null> {
    const Context = audioContextConstructor();
    if (Context === null || typeof globalSlot("AudioWorkletNode") !== "function") {
      return {
        code: "unsupported",
        detail: "the browser offers no AudioContext with an AudioWorklet",
        recoverable: false,
      };
    }
    const devices = navigator.mediaDevices;
    if (typeof devices?.getUserMedia !== "function") {
      return {
        code: "unsupported",
        detail: "the browser offers no navigator.mediaDevices.getUserMedia",
        recoverable: false,
      };
    }
    let stream: MediaStream;
    try {
      stream = await devices.getUserMedia(MICROPHONE_CONSTRAINTS);
    } catch (problem) {
      return microphoneProblem(problem);
    }
    this.stream = stream;
    // Asking for the payload's own rate is what turns the resampler into a pass-through on the
    // browsers that honour it; `context.sampleRate` below says what this one actually gave. A
    // browser also refuses the context itself -- one that will not run at that rate, or one that has
    // handed out every hardware context it has -- and that refusal is the page's to explain.
    let context: AudioContext;
    try {
      context = new Context({ sampleRate: this.targetRate, latencyHint: "interactive" });
    } catch (problem) {
      return { code: "unavailable", detail: describe(problem), recoverable: false };
    }
    this.context = context;
    try {
      await context.resume();
      await context.audioWorklet.addModule(this.workletUrl);
    } catch (problem) {
      return { code: "unavailable", detail: describe(problem), recoverable: false };
    }
    this.clockOriginMs = Date.now() - context.currentTime * 1000;
    this.resampler = new AudioResampler(context.sampleRate, this.targetRate);
    const node = new AudioWorkletNode(context, PCM_WORKLET_PROCESSOR, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
      outputChannelCount: [1],
      processorOptions: { chunkMs: this.chunkMs } satisfies PcmWorkletOptions,
    });
    node.port.onmessage = (event) => this.onChunk(event);
    this.node = node;
    this.source = context.createMediaStreamSource(stream);
    this.source.connect(node);
    // The destination end of the graph is what keeps a browser pulling it; the processor's own
    // output is silence, so the microphone never reaches the student's speakers.
    node.connect(context.destination);
    for (const track of stream.getAudioTracks()) {
      track.addEventListener("ended", this.onTrackEnded);
    }
    return null;
  }

  private onChunk(event: MessageEvent): void {
    const resampler = this.resampler;
    if (!this.running || resampler === null) return;
    const chunk = event.data as PcmWorkletChunk;
    const samples = resampler.push(chunk.samples);
    if (samples.length === 0) return;
    this.sink.sendAudio(
      encodeAudioFrame({
        seq: this.seq,
        clientTimeMs: this.clientTimeMs(chunk.startTimeSec),
        pcm: pcm16Bytes(samples),
      }),
    );
    this.seq += 1;
  }

  /**
   * The client-clock capture time of a chunk's first sample. The processor stamps a chunk with the
   * audio clock, which never steps, and the origin measured when the graph started ties that clock
   * to `Date.now()`, so a frame carries the sample's own time and not the moment the main thread
   * got round to reading it -- a React render can delay that by tens of ms.
   */
  private clientTimeMs(startTimeSec: number): number {
    return Math.max(0, Math.round(this.clockOriginMs + startTimeSec * 1000));
  }

  private readonly onTrackEnded = (): void => {
    // A USB headset unplugged, or another tab taking the microphone: nothing this client could
    // retry brings it back, so the student is told instead of watching a transcript that has
    // quietly stopped moving.
    this.giveUp({
      code: "unavailable",
      detail: "the microphone track ended",
      recoverable: false,
    });
  };

  /** Ends streaming for good and tells the page why. */
  private giveUp(problem: TranscriberProblem): void {
    this.abandon();
    this.callbacks.onProblem?.(problem);
  }

  private abandon(): void {
    this.running = false;
    this.resampler = null;
    const node = this.node;
    this.node = null;
    if (node !== null) {
      node.port.onmessage = null;
      node.port.close();
      node.disconnect();
    }
    this.source?.disconnect();
    this.source = null;
    const context = this.context;
    this.context = null;
    if (context !== null) void context.close();
    const stream = this.stream;
    this.stream = null;
    if (stream === null) return;
    for (const track of stream.getTracks()) {
      track.removeEventListener("ended", this.onTrackEnded);
      track.stop();
    }
  }
}
