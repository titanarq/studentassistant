/**
 * The AudioWorklet processor that captures the microphone for server-side STT (#40).
 *
 * This file runs in the worklet's own global scope, on the audio thread, which `addModule()` loads
 * from the URL Vite builds for it (`?worker&url`, in `audioStreamTranscriber.ts`). The main thread
 * imports it for the processor's name and the shape of the chunk it posts, and never runs it: the
 * scope's globals (`AudioWorkletProcessor`, `registerProcessor`, `currentFrame`, `sampleRate`) only
 * exist there, so they are declared here and the registration is guarded by one of them.
 *
 * The processor's own job is the small one -- downmix a render quantum to mono, collect quanta into
 * a chunk worth one frame, and post it. Resampling and the frame header stay on the main thread
 * (`audioFrames.ts`), so the audio thread's budget of 128 samples goes to the sound card.
 */

import { downmixToMono } from "./audioFrames";

/** The name the processor registers under and the main thread's `AudioWorkletNode` asks for. */
export const PCM_WORKLET_PROCESSOR = "sa-pcm-capture";

/** How much audio one posted chunk carries, and so how much one frame on the wire carries. */
export const PCM_WORKLET_CHUNK_MS = 100;

/** One chunk of captured microphone audio, as the processor posts it to the main thread. */
export interface PcmWorkletChunk {
  /** Mono Float32 samples in [-1, 1], at the worklet scope's own `sampleRate`. */
  readonly samples: Float32Array;
  /** The worklet's frame index of the chunk's first sample over its `sampleRate`: seconds. */
  readonly startTimeSec: number;
}

/** What the main thread's node passes down as `processorOptions`. */
export interface PcmWorkletOptions {
  /** How much audio one chunk carries; `PCM_WORKLET_CHUNK_MS` by default. */
  readonly chunkMs?: number;
}

interface WorkletProcessor {
  readonly port: MessagePort;
}

interface WorkletProcessorConstructor {
  new (options?: { processorOptions?: PcmWorkletOptions }): WorkletProcessor;
}

// The worklet scope's globals. TypeScript's DOM lib puts them on `AudioWorkletGlobalScope`, which
// is no module's scope, so they are declared here instead -- and only ever read inside the guard
// below, because on the main thread none of them exists.
declare const AudioWorkletProcessor: WorkletProcessorConstructor;
declare const registerProcessor: (name: string, ctor: WorkletProcessorConstructor) => void;
declare const currentFrame: number;
declare const sampleRate: number;

if (typeof registerProcessor === "function" && typeof AudioWorkletProcessor === "function") {
  class PcmCaptureProcessor extends AudioWorkletProcessor {
    /** How many samples make one chunk, at this scope's own rate. */
    private readonly chunkSamples: number;
    /** The samples collected since the last chunk went out. */
    private pending: number[] = [];
    /** The frame index of `pending`'s first sample, which is the chunk's capture time. */
    private pendingStartFrame = 0;

    constructor(options?: { processorOptions?: PcmWorkletOptions }) {
      super(options);
      const chunkMs = options?.processorOptions?.chunkMs ?? PCM_WORKLET_CHUNK_MS;
      this.chunkSamples = Math.max(1, Math.round((sampleRate * chunkMs) / 1000));
    }

    process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
      const quantum = inputs[0];
      if (quantum !== undefined && quantum.length > 0) this.collect(quantum);
      // The node's output ends at the destination, which is what keeps a browser pulling the graph;
      // it stays silent, so the microphone never reaches the student's own speakers.
      for (const output of outputs) {
        for (const channel of output) channel.fill(0);
      }
      // Still capturing: the microphone is a live source with no tail to wait for, and `false`
      // would let the browser stop calling this and end the session's audio.
      return true;
    }

    private collect(quantum: Float32Array[]): void {
      if (this.pending.length === 0) this.pendingStartFrame = currentFrame;
      const mono = downmixToMono(quantum);
      for (let index = 0; index < mono.length; index += 1) this.pending.push(mono[index]);
      while (this.pending.length >= this.chunkSamples) {
        const samples = Float32Array.from(this.pending.splice(0, this.chunkSamples));
        const chunk: PcmWorkletChunk = {
          samples,
          startTimeSec: this.pendingStartFrame / sampleRate,
        };
        // The buffer travels with the message instead of being copied: ten chunks a second of a
        // laptop microphone is 192 kB a minute at 48 kHz.
        this.port.postMessage(chunk, [samples.buffer]);
        this.pendingStartFrame += this.chunkSamples;
      }
    }
  }

  registerProcessor(PCM_WORKLET_PROCESSOR, PcmCaptureProcessor);
}
