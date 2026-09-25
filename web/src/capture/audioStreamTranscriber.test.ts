/**
 * The server-side STT path against the fake AudioContext and the fake socket (#40). The microphone
 * is driven by posting the chunks the worklet processor posts, and what goes out is read back as the
 * binary frame protocol/README.md defines, so the test asserts the wire instead of this client's own
 * idea of it. The clock is frozen, which is what makes a frame's capture time a number to name.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type HelloAck, parseServerEvent, PROTOCOL_VERSION } from "../protocol";
import { sharedExamples } from "../test/protocolExamples";
import { AUDIO_HEADER_SIZE, PCM_SAMPLE_RATE_HZ, PCM_SAMPLE_WIDTH_BYTES } from "./audioFrames";
import {
  AUDIO_STREAM_PROVIDER,
  audioStreamSupported,
  AudioStreamTranscriber,
  type AudioStreamTranscriberOptions,
} from "./audioStreamTranscriber";
import pcmWorkletUrl from "./pcmWorklet.ts?worker&url";
import { PCM_WORKLET_CHUNK_MS, PCM_WORKLET_PROCESSOR, type PcmWorkletChunk } from "./pcmWorklet";
import { type ClientSegment, CLIENT_AUDIO_FORMAT, SessionSocket } from "./sessionSocket";
import { type TranscriberProblem, TranscriberError } from "./transcriber";
import {
  type CaptureFakes,
  type FakeAudioContextOptions,
  type FakeAudioWorkletNode,
  type FakeWebSocket,
  FakeAudioContext,
  FakeAudioWorklet,
  installCaptureFakes,
  swapGlobal,
} from "./testing";

const WS_PATH = "/ws/sessions/s-20260924-1810";
/** The frozen client clock; a frame's capture time is this plus the chunk's own offset. */
const NOW_MS = 1790251200000;
/** A laptop sound card's usual rate, which the payload's 16 kHz is a third of. */
const HARDWARE_RATE = 48_000;

interface Harness {
  readonly socket: SessionSocket;
  readonly fake: FakeWebSocket;
  readonly transcriber: AudioStreamTranscriber;
  /** Every problem the transcriber reported, in order. */
  readonly problems: TranscriberProblem[];
  /** Every segment it emitted, which the server STT mode never does. */
  readonly segments: Array<[string, ClientSegment]>;
}

let fakes: CaptureFakes;
const restores: Array<() => void> = [];

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW_MS);
});

afterEach(() => {
  while (restores.length > 0) restores.pop()?.();
  vi.useRealTimers();
});

function installFakes(): CaptureFakes {
  const installed = installCaptureFakes();
  fakes = installed;
  restores.push(installed.restore);
  return installed;
}

/** The shared `hello.ack` example, as the backend answers a client it wants audio from. */
function serverAck(): HelloAck {
  const example = sharedExamples().find((entry) => entry.name === "server.hello.ack");
  const ack = parseServerEvent({
    ...(example?.json as object),
    stt_mode: "server",
    audio_format: CLIENT_AUDIO_FORMAT,
  });
  if (ack.type !== "hello.ack") throw new Error("the shared example is no hello.ack");
  return ack;
}

/** A session socket the backend chose server STT on, and the transcriber built on it, not started. */
async function buildSession(
  options: Partial<AudioStreamTranscriberOptions> = {},
): Promise<Harness> {
  const installed = installFakes();
  const problems: TranscriberProblem[] = [];
  const segments: Array<[string, ClientSegment]> = [];
  const socket = new SessionSocket({ wsPath: WS_PATH, clientTimeMs: NOW_MS });
  const fake = installed.sockets[0];
  fake.serverOpen();
  fake.serverMessage(JSON.stringify(serverAck()));
  const handshake = await socket.handshake;
  if (handshake.kind !== "ok") throw new Error(`the backend's hello.ack was refused: ${handshake.problem}`);
  const transcriber = new AudioStreamTranscriber(
    {
      onSegment: (segment, kind) => segments.push([kind, segment]),
      onProblem: (problem) => problems.push(problem),
    },
    { sink: socket, ...options },
  );
  return { socket, fake, transcriber, problems, segments };
}

/** `buildSession` plus a `start()` that got as far as streaming. */
async function startStreaming(
  options: Partial<AudioStreamTranscriberOptions> = {},
  hardwareRate: number | null = HARDWARE_RATE,
): Promise<Harness> {
  const harness = await buildSession(options);
  if (hardwareRate !== null) installHardwareRate(hardwareRate);
  await harness.transcriber.start();
  return harness;
}

/** A browser that gives the sound card's own rate instead of the one the context was asked for. */
function installHardwareRate(sampleRate: number): void {
  class HardwareRateAudioContext extends FakeAudioContext {
    constructor(options?: FakeAudioContextOptions) {
      super({ ...options, sampleRate });
    }
  }
  restores.push(swapGlobal("AudioContext", HardwareRateAudioContext));
}

/** A processor module that does not compile, which is what `addModule` rejecting stands for. */
function installWorkletFailure(message: string): void {
  const addModule = FakeAudioWorklet.prototype.addModule;
  FakeAudioWorklet.prototype.addModule = async () => {
    throw new DOMException(message, "AbortError");
  };
  restores.push(() => {
    FakeAudioWorklet.prototype.addModule = addModule;
  });
}

/** The worklet node the transcriber built, which is the processor's end of the message port. */
function workletNode(): FakeAudioWorkletNode {
  const node = fakes.workletNodes[0];
  if (node === undefined) throw new Error("the transcriber built no worklet node");
  return node;
}

/** The processor posting one chunk of captured microphone audio to the main thread. */
function capture(samples: ArrayLike<number>, startTimeSec: number): void {
  const chunk: PcmWorkletChunk = { samples: Float32Array.from(samples), startTimeSec };
  workletNode().emitProcessorMessage(chunk);
}

interface ReadFrame {
  readonly bytes: Uint8Array;
  readonly magic: string;
  readonly version: string;
  readonly seq: number;
  readonly clientTimeMs: number;
  readonly samples: number[];
}

/** One binary frame the socket sent, read back the way protocol/README.md's table lays it out. */
function frameOf(fake: FakeWebSocket, index = 0): ReadFrame {
  const bytes = fake.sentBinary[index];
  if (bytes === undefined) throw new Error(`the socket sent no binary frame ${index}`);
  const header = new DataView(bytes.buffer, bytes.byteOffset, AUDIO_HEADER_SIZE);
  return {
    bytes,
    magic: String.fromCharCode(...bytes.subarray(0, 4)),
    version: `${header.getUint8(4)}.${header.getUint8(5)}`,
    seq: header.getUint32(6, false),
    clientTimeMs: Number(header.getBigUint64(10, false)),
    samples: payloadOf(bytes),
  };
}

/** A frame's payload as the little-endian signed samples it is. */
function payloadOf(bytes: Uint8Array): number[] {
  const view = new DataView(
    bytes.buffer,
    bytes.byteOffset + AUDIO_HEADER_SIZE,
    bytes.byteLength - AUDIO_HEADER_SIZE,
  );
  const samples: number[] = [];
  for (let at = 0; at < view.byteLength; at += PCM_SAMPLE_WIDTH_BYTES) {
    samples.push(view.getInt16(at, true));
  }
  return samples;
}

/** A ramp in eighths, which Float32 holds exactly, so an assertion can name its samples. */
function ramp(length: number): Float32Array {
  const signal = new Float32Array(length);
  for (let index = 0; index < length; index += 1) signal[index] = index / 128;
  return signal;
}

/** A tenth of a second of a voice's fundamental, at the rate the microphone captured it. */
function tone(length: number): Float32Array {
  const signal = new Float32Array(length);
  for (let index = 0; index < length; index += 1) {
    signal[index] = Math.sin((2 * Math.PI * 440 * index) / HARDWARE_RATE);
  }
  return signal;
}

/** What `start()` rejected with, which is the problem it also reported to the page. */
async function refused(transcriber: AudioStreamTranscriber): Promise<TranscriberProblem> {
  try {
    await transcriber.start();
  } catch (problem) {
    expect(problem).toBeInstanceOf(TranscriberError);
    return problem as TranscriberProblem;
  }
  throw new Error("start() resolved instead of refusing");
}

describe("start", () => {
  it("takes the microphone, loads the processor and builds the graph that streams it", async () => {
    const harness = await startStreaming({}, null);

    expect(harness.socket.sttMode).toBe("server");
    expect(harness.socket.audioFormat).toEqual(CLIENT_AUDIO_FORMAT);
    expect(fakes.devices.getUserMediaCalls).toEqual([
      {
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      },
    ]);

    const context = fakes.contexts[0];
    // The context is asked for the payload's own rate, which makes resampling a pass-through on the
    // browsers that honour it.
    expect(context.sampleRate).toBe(PCM_SAMPLE_RATE_HZ);
    expect(context.state).toBe("running");
    expect(context.audioWorklet.moduleUrls).toEqual([pcmWorkletUrl]);

    const node = workletNode();
    expect(node.processorName).toBe(PCM_WORKLET_PROCESSOR);
    expect(node.options).toMatchObject({
      numberOfInputs: 1,
      channelCount: 1,
      processorOptions: { chunkMs: PCM_WORKLET_CHUNK_MS },
    });
    expect(context.sources[0].node.connections).toContain(node);
    expect(node.connections).toContain(context.destination);
  });

  it("loads the processor module it is told to, for a page that serves its own", async () => {
    await startStreaming({ workletUrl: "/capture/pcmWorklet.js" }, null);

    expect(fakes.contexts[0].audioWorklet.moduleUrls).toEqual(["/capture/pcmWorklet.js"]);
  });

  it("streams nothing until the processor has captured a chunk", async () => {
    const harness = await startStreaming();

    expect(harness.fake.sentBinary).toEqual([]);
    // `hello` is the only text frame; audio goes out as bytes and nothing else does.
    expect(harness.fake.sentText.length).toBe(1);
  });

  it("starts once, however often the page asks", async () => {
    const harness = await startStreaming();

    await harness.transcriber.start();

    expect(fakes.contexts.length).toBe(1);
    expect(fakes.devices.getUserMediaCalls.length).toBe(1);
  });

  it("resamples what a sound card of another rate gives instead of trusting the request", async () => {
    const harness = await startStreaming({}, HARDWARE_RATE);

    expect(fakes.contexts[0].sampleRate).toBe(HARDWARE_RATE);
    capture(ramp(12), 0);
    expect(frameOf(harness.fake).samples).toEqual([0, 768, 1536, 2304]);
  });
});

describe("the frames it sends", () => {
  it("sends one per chunk, counting seq from 0 and carrying the chunk's own capture time", async () => {
    const harness = await startStreaming();

    capture(tone(4800), 0);
    capture(tone(4800), 0.1);
    capture(tone(4800), 0.2);

    expect(harness.fake.sentBinary.length).toBe(3);
    expect([0, 1, 2].map((index) => frameOf(harness.fake, index).seq)).toEqual([0, 1, 2]);
    expect([0, 1, 2].map((index) => frameOf(harness.fake, index).clientTimeMs)).toEqual([
      NOW_MS,
      NOW_MS + 100,
      NOW_MS + 200,
    ]);
  });

  it("writes the header and the payload protocol/README.md defines", async () => {
    const harness = await startStreaming();

    // A tenth of a second at 48 kHz is a third as many samples in the 16 kHz payload.
    capture(tone(4800), 0.5);
    const frame = frameOf(harness.fake);

    expect(frame.magic).toBe("SAAF");
    expect(frame.version).toBe(PROTOCOL_VERSION);
    expect(frame.clientTimeMs).toBe(NOW_MS + 500);
    expect(frame.samples.length).toBe(1600);
    expect(frame.bytes.byteLength).toBe(AUDIO_HEADER_SIZE + 1600 * PCM_SAMPLE_WIDTH_BYTES);
  });

  it("holds a chunk back until it settles a sample of the payload", async () => {
    const harness = await startStreaming();

    capture(ramp(1), 0);
    expect(harness.fake.sentBinary).toEqual([]);

    capture(ramp(2), 0.002);
    expect(harness.fake.sentBinary.length).toBe(1);
    expect(frameOf(harness.fake).samples).toEqual([0]);
  });

  it("emits no segment and runs no recognition of its own: the transcript comes back", async () => {
    const harness = await startStreaming();

    capture(tone(4800), 0);

    expect(harness.segments).toEqual([]);
    expect(fakes.recognitions).toEqual([]);
    expect(harness.transcriber.provider).toBe(AUDIO_STREAM_PROVIDER);
  });
});

describe("stop", () => {
  it("takes the graph apart, closes the context and releases the microphone", async () => {
    const harness = await startStreaming();

    harness.transcriber.stop();

    const context = fakes.contexts[0];
    const node = workletNode();
    expect(context.closeCount).toBe(1);
    expect(node.disconnectCount).toBe(1);
    expect(node.port.closeCount).toBe(1);
    expect(context.sources[0].node.disconnectCount).toBe(1);
    expect(fakes.audioTrack.stopCount).toBe(1);
    expect(fakes.audioTrack.readyState).toBe("ended");

    capture(tone(4800), 0.3);
    expect(harness.fake.sentBinary).toEqual([]);
  });

  it("reports a microphone that goes away mid-session and stops streaming", async () => {
    const harness = await startStreaming();

    fakes.audioTrack.endFromDevice();

    expect(harness.problems).toEqual([
      { code: "unavailable", detail: "the microphone track ended", recoverable: false },
    ]);
    expect(fakes.contexts[0].closeCount).toBe(1);

    capture(tone(4800), 0.3);
    expect(harness.fake.sentBinary).toEqual([]);
  });
});

describe("a browser or a student that will not give a microphone", () => {
  it("reports a refused microphone as the page's own permission problem", async () => {
    const harness = await buildSession();
    fakes.devices.failure = "NotAllowedError";

    expect(await refused(harness.transcriber)).toMatchObject({
      code: "permission-denied",
      recoverable: false,
    });
    expect(harness.problems.map((problem) => problem.code)).toEqual(["permission-denied"]);
    expect(fakes.contexts).toEqual([]);
  });

  it("reports a laptop with no microphone as unavailable, not as a refusal", async () => {
    const harness = await buildSession();
    fakes.devices.failure = "NotFoundError";

    const problem = await refused(harness.transcriber);

    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("NotFoundError");
  });

  it("reports a browser with no AudioContext as unsupported", async () => {
    const harness = await buildSession();
    restores.push(swapGlobal("AudioContext", undefined));

    expect(audioStreamSupported()).toBe(false);
    expect(await refused(harness.transcriber)).toMatchObject({
      code: "unsupported",
      recoverable: false,
    });
    expect(harness.fake.sentBinary).toEqual([]);
  });

  it("says a browser that can stream audio can, so hello announces the format", () => {
    installFakes();

    expect(audioStreamSupported()).toBe(true);
  });

  it("releases the microphone when the processor itself will not load", async () => {
    installWorkletFailure("the processor does not compile");
    const harness = await buildSession();

    const problem = await refused(harness.transcriber);

    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("the processor does not compile");
    expect(fakes.audioTrack.stopCount).toBe(1);
    expect(fakes.contexts[0].closeCount).toBe(1);
    expect(harness.fake.sentBinary).toEqual([]);
  });

  it("reports a browser that refuses the audio context itself, and still gives the microphone back", async () => {
    const harness = await buildSession();
    restores.push(
      swapGlobal(
        "AudioContext",
        class {
          constructor() {
            throw new DOMException("the number of hardware contexts is at its maximum", "NotSupportedError");
          }
        },
      ),
    );

    const problem = await refused(harness.transcriber);

    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("hardware contexts");
    expect(fakes.contexts).toEqual([]);
    // The microphone was already this client's when the context was refused, so it goes back.
    expect(fakes.audioTrack.stopCount).toBe(1);
    expect(harness.fake.sentBinary).toEqual([]);
  });
});
