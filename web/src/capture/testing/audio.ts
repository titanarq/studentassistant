/**
 * Fakes of the Web Audio APIs the server-side STT path uses (#40): `AudioContext`,
 * `AudioWorkletNode` and the message port that carries captured microphone audio from the worklet
 * processor back to the main thread. A test pushes chunks with `emitProcessorMessage` instead of
 * speaking into a microphone.
 */

import { FakeEventTarget, swapGlobal } from "./support";

/** A laptop microphone's usual rate; the capture code resamples whatever it gets to 16 kHz. */
const DEFAULT_SAMPLE_RATE = 48_000;

let activeContexts: FakeAudioContext[] = [];
let activeWorkletNodes: FakeAudioWorkletNode[] = [];

export class FakeAudioNode {
  /** What this node was connected to, in order. */
  readonly connections: FakeAudioNode[] = [];
  disconnectCount = 0;

  constructor(readonly context: FakeAudioContext) {}

  connect(destination: FakeAudioNode): FakeAudioNode {
    this.connections.push(destination);
    return destination;
  }

  disconnect(): void {
    this.disconnectCount += 1;
    this.connections.length = 0;
  }
}

/**
 * The port between the worklet processor and the main thread. Unlike a real port it delivers
 * messages whether or not the code called `start()`, so a test never waits on the wrong thing.
 */
export class FakeMessagePort extends FakeEventTarget {
  /** Messages the main thread posted towards the processor, in order. */
  readonly posted: unknown[] = [];
  startCount = 0;
  closeCount = 0;
  onmessage: ((event: MessageEvent) => void) | null = null;

  postMessage(data: unknown): void {
    this.posted.push(data);
  }

  start(): void {
    this.startCount += 1;
  }

  close(): void {
    this.closeCount += 1;
  }

  /** Test control: the processor posted a chunk of captured audio to the main thread. */
  emitMessage(data: unknown): void {
    this.dispatch(new MessageEvent("message", { data }));
  }
}

/** The `AudioWorkletNode` options the capture code passes to its processor. */
export interface FakeAudioWorkletNodeOptions {
  numberOfInputs?: number;
  numberOfOutputs?: number;
  numberOfChannels?: number;
  channelCount?: number;
  outputChannelCount?: number[];
  processorOptions?: Record<string, unknown>;
}

export class FakeAudioWorkletNode extends FakeAudioNode {
  readonly port = new FakeMessagePort();
  readonly processorName: string;
  readonly options: FakeAudioWorkletNodeOptions;

  constructor(
    context: FakeAudioContext,
    processorName: string,
    options: FakeAudioWorkletNodeOptions = {},
  ) {
    super(context);
    this.processorName = processorName;
    this.options = options;
    activeWorkletNodes.push(this);
  }

  /** Test control: the processor captured a chunk and posted it to the main thread. */
  emitProcessorMessage(data: unknown): void {
    this.port.emitMessage(data);
  }
}

export class FakeAudioWorklet {
  /** Every module URL `addModule()` was asked for, in order. */
  readonly moduleUrls: string[] = [];
  /** A message that makes `addModule()` reject, e.g. a processor that does not compile. */
  failure: string | null = null;

  async addModule(url: string | URL): Promise<void> {
    if (this.failure !== null) throw new DOMException(this.failure, "AbortError");
    this.moduleUrls.push(String(url));
  }
}

export interface FakeAudioContextOptions {
  sampleRate?: number;
  latencyHint?: string | number;
}

export class FakeAudioContext {
  readonly sampleRate: number;
  readonly destination: FakeAudioNode;
  readonly audioWorklet = new FakeAudioWorklet();
  /** Every stream `createMediaStreamSource()` was given, with the node built for it. */
  readonly sources: Array<{ stream: MediaStream; node: FakeAudioNode }> = [];
  /** A real context starts suspended until the page resumes it after a user gesture. */
  state: "suspended" | "running" | "closed" = "suspended";
  currentTime = 0;
  resumeCount = 0;
  suspendCount = 0;
  closeCount = 0;

  constructor(options: FakeAudioContextOptions = {}) {
    this.sampleRate = options.sampleRate ?? DEFAULT_SAMPLE_RATE;
    this.destination = new FakeAudioNode(this);
    activeContexts.push(this);
  }

  createMediaStreamSource(stream: MediaStream): FakeAudioNode {
    const node = new FakeAudioNode(this);
    this.sources.push({ stream, node });
    return node;
  }

  async resume(): Promise<void> {
    this.resumeCount += 1;
    if (this.state !== "closed") this.state = "running";
  }

  async suspend(): Promise<void> {
    this.suspendCount += 1;
    if (this.state !== "closed") this.state = "suspended";
  }

  async close(): Promise<void> {
    this.closeCount += 1;
    this.state = "closed";
  }
}

export interface AudioFakes {
  /** Every context the code under test built, in order. */
  readonly contexts: FakeAudioContext[];
  /** Every worklet node the code under test built, in order. */
  readonly workletNodes: FakeAudioWorkletNode[];
  restore(): void;
}

/** Replaces the global `AudioContext` and `AudioWorkletNode` with the fakes. */
export function installAudioFakes(): AudioFakes {
  activeContexts = [];
  activeWorkletNodes = [];
  const restores = [
    swapGlobal("AudioContext", FakeAudioContext),
    swapGlobal("AudioWorkletNode", FakeAudioWorkletNode),
  ];
  return {
    contexts: activeContexts,
    workletNodes: activeWorkletNodes,
    restore: () => {
      for (const restore of restores.reverse()) restore();
    },
  };
}
