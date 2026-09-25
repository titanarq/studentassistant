/**
 * The worklet processor (#40), loaded into a faked `AudioWorkletGlobalScope`. The processor only
 * exists on the audio thread, so this test puts the scope's four globals in place, imports the
 * module for the first time and takes the class `registerProcessor` was handed -- which is also
 * what proves the module registers the processor under the name the main thread asks for.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  PCM_WORKLET_CHUNK_MS,
  PCM_WORKLET_PROCESSOR,
  type PcmWorkletChunk,
  type PcmWorkletOptions,
} from "./pcmWorklet";
import { swapGlobal } from "./testing";

/** The render quantum a worklet is called with, in samples. */
const QUANTUM = 128;
/** A laptop sound card's rate; a tenth of a second of it is 37 and a half quanta. */
const SAMPLE_RATE = 48_000;
/** Every sample is its own index over this, so Float32 holds it exactly and a test can name it. */
const SCALE = 65_536;

interface Processor {
  process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean;
}

type ProcessorConstructor = new (options?: { processorOptions?: PcmWorkletOptions }) => Processor;

interface Posted {
  readonly chunk: PcmWorkletChunk;
  readonly transfer: readonly unknown[];
}

/** What the processor posted to the main thread, in order. */
let posted: Posted[] = [];
/** The processors `registerProcessor` was given, by name. */
let registered: Map<string, ProcessorConstructor>;
const restores: Array<() => void> = [];

beforeEach(() => {
  posted = [];
  registered = new Map();
});

afterEach(() => {
  while (restores.length > 0) restores.pop()?.();
  vi.resetModules();
});

/** The worklet scope's `AudioWorkletProcessor`: a base whose port records what it is given. */
class FakeWorkletProcessor {
  readonly port = {
    postMessage: (chunk: PcmWorkletChunk, transfer: Transferable[]): void => {
      posted.push({ chunk, transfer });
    },
  };
}

/** Puts the scope's globals in place and imports the processor module against them. */
async function loadProcessor(sampleRate = SAMPLE_RATE): Promise<void> {
  restores.push(
    swapGlobal("AudioWorkletProcessor", FakeWorkletProcessor),
    swapGlobal("registerProcessor", (name: string, ctor: ProcessorConstructor) => {
      registered.set(name, ctor);
    }),
    swapGlobal("sampleRate", sampleRate),
    swapGlobal("currentFrame", 0),
  );
  vi.resetModules();
  await import("./pcmWorklet");
}

function processor(chunkMs?: number): Processor {
  const Constructor = registered.get(PCM_WORKLET_PROCESSOR);
  if (Constructor === undefined) throw new Error("the module registered no processor");
  return new Constructor(chunkMs === undefined ? undefined : { processorOptions: { chunkMs } });
}

/** The frame position the worklet scope reports for the quantum being run. */
function setCurrentFrame(frame: number): void {
  (globalThis as unknown as Record<string, number>).currentFrame = frame;
}

/**
 * Runs the quantum a worklet would run at `index`: the ramp's own next 128 samples, at the frame
 * position the scope reports for it. Every channel carries the same ramp, so the mono a downmix of
 * them produces is that ramp again, and a test can name its samples.
 */
function runQuantum(target: Processor, index: number, channels = 1): boolean {
  setCurrentFrame(QUANTUM * index);
  const input: Float32Array[] = [];
  for (let channel = 0; channel < channels; channel += 1) {
    const quantum = new Float32Array(QUANTUM);
    for (let sample = 0; sample < QUANTUM; sample += 1) {
      quantum[sample] = (index * QUANTUM + sample) / SCALE;
    }
    input.push(quantum);
  }
  const output = [new Float32Array(QUANTUM).fill(1)];
  const stillCapturing = target.process([input], [output]);
  expect(output[0], "the processor must not put the microphone on the speakers").toEqual(
    new Float32Array(QUANTUM),
  );
  return stillCapturing;
}

describe("the module in a worklet scope", () => {
  it("registers the processor under the name the main thread asks for", async () => {
    await loadProcessor();

    expect([...registered.keys()]).toEqual([PCM_WORKLET_PROCESSOR]);
  });

  it("registers nothing on the main thread, which has no such globals", async () => {
    const module = await import("./pcmWorklet");

    expect(module.PCM_WORKLET_PROCESSOR).toBe("sa-pcm-capture");
    expect(registered.size).toBe(0);
  });
});

describe("the processor", () => {
  it("posts a chunk once a tenth of a second of audio is in, and not before", async () => {
    await loadProcessor();
    const capturing = processor();

    for (let index = 0; index < 37; index += 1) expect(runQuantum(capturing, index)).toBe(true);
    expect(posted).toEqual([]);

    expect(runQuantum(capturing, 37)).toBe(true);
    expect(posted.length).toBe(1);
    expect(posted[0].chunk.samples.length).toBe(Math.round((SAMPLE_RATE * PCM_WORKLET_CHUNK_MS) / 1000));
  });

  it("keeps the audio whole across chunks and stamps each with its own capture time", async () => {
    await loadProcessor();
    const capturing = processor();

    // 76 quanta are two chunks of 4800 samples with a quantum's remainder still held back.
    for (let index = 0; index < 76; index += 1) runQuantum(capturing, index);

    expect(posted.length).toBe(2);
    const [first, second] = posted;
    expect(first.chunk.samples[0]).toBe(0);
    expect(first.chunk.samples[4799]).toBe(4799 / SCALE);
    expect(first.chunk.startTimeSec).toBe(0);
    // The second chunk starts where the first ended, not where the quantum that completed it did.
    expect(second.chunk.samples[0]).toBe(4800 / SCALE);
    expect(second.chunk.startTimeSec).toBe(4800 / SAMPLE_RATE);
  });

  it("hands the chunk's buffer over instead of a copy of it", async () => {
    await loadProcessor();
    const capturing = processor();

    for (let index = 0; index < 38; index += 1) runQuantum(capturing, index);

    expect(posted[0].transfer).toEqual([posted[0].chunk.samples.buffer]);
  });

  it("downmixes a stereo quantum to the mono a frame carries", async () => {
    await loadProcessor();
    const capturing = processor(10);

    // 480 samples are a tenth of a second at 48 kHz, so one chunk comes out of four quanta.
    for (let index = 0; index < 4; index += 1) runQuantum(capturing, index, 2);

    expect(posted.length).toBe(1);
    expect(posted[0].chunk.samples.length).toBe(480);
    expect(posted[0].chunk.samples[7]).toBe(7 / SCALE);
  });

  it("collects a chunk of the length the main thread asked for", async () => {
    await loadProcessor();
    const capturing = processor(20);

    for (let index = 0; index < 8; index += 1) runQuantum(capturing, index);

    expect(posted.map((entry) => entry.chunk.samples.length)).toEqual([960]);
    expect(posted[0].chunk.startTimeSec).toBe(0);
  });

  it("carries on when a quantum brings no channel, as a muted microphone does", async () => {
    await loadProcessor();
    const capturing = processor();

    expect(capturing.process([[]], [[new Float32Array(QUANTUM).fill(1)]])).toBe(true);
    expect(posted).toEqual([]);
  });
});
