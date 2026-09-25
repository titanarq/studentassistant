/**
 * The binary audio frames of protocol v1 (#40, protocol/README.md "Binary audio frames") and the
 * arithmetic the microphone's audio needs to become one. A worklet hands over Float32 samples at
 * whatever rate the sound card runs; a frame's payload is PCM16, 16 kHz, mono, little-endian, under
 * an 18-byte big-endian header. Nothing here touches a browser API, so the layout the backend's own
 * `decode_frame` reads and the resampling a twenty-minute session depends on are both testable on
 * their own.
 */

import { PROTOCOL_VERSION, parseVersion } from "../protocol";

/** The four ASCII bytes every frame starts with. */
export const AUDIO_MAGIC = "SAAF";

/** The header's size in bytes; the payload follows it. */
export const AUDIO_HEADER_SIZE = 18;

/** PCM16: two bytes per sample. */
export const PCM_SAMPLE_WIDTH_BYTES = 2;

/** The sample rate every frame's payload carries, which protocol v1 fixes. */
export const PCM_SAMPLE_RATE_HZ = 16_000;

/** A frame that does not follow the documented layout, and would go out corrupt if it did. */
export class AudioFrameError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AudioFrameError";
  }
}

/** One frame to send, in the fields protocol/README.md names. */
export interface AudioFrame {
  /** The per-session frame counter, from 0; the server `ack` reports the highest one it got. */
  readonly seq: number;
  /** The client-clock capture time of the payload's first sample, in ms. */
  readonly clientTimeMs: number;
  /** The payload: PCM16, 16 kHz, mono, little-endian, a whole number of samples. */
  readonly pcm: Uint8Array<ArrayBuffer>;
  /** The version both sides speak; this client's own by default. */
  readonly protocolVersion?: string;
}

const U8_MAX = 0xff;
const U32_MAX = 0xffff_ffff;
const U64_MAX = (1n << 64n) - 1n;

/**
 * Header followed by payload, ready to send as one binary WebSocket message. The header is
 * big-endian and the samples inside the payload are little-endian, as the two tables in
 * protocol/README.md say.
 */
export function encodeAudioFrame(frame: AudioFrame): Uint8Array<ArrayBuffer> {
  const version = frame.protocolVersion ?? PROTOCOL_VERSION;
  const [major, minor] = parseVersion(version);
  checkFrame(frame, major, minor);
  const bytes = new Uint8Array(new ArrayBuffer(AUDIO_HEADER_SIZE + frame.pcm.byteLength));
  const header = new DataView(bytes.buffer);
  for (let offset = 0; offset < AUDIO_MAGIC.length; offset += 1) {
    header.setUint8(offset, AUDIO_MAGIC.charCodeAt(offset));
  }
  header.setUint8(4, major);
  header.setUint8(5, minor);
  header.setUint32(6, frame.seq, false);
  header.setBigUint64(10, BigInt(frame.clientTimeMs), false);
  bytes.set(frame.pcm, AUDIO_HEADER_SIZE);
  return bytes;
}

/**
 * The header's own bounds, the ones the backend checks on arrival: a value outside them would be
 * written wrapped, so a frame nobody can make sense of, and the mistake that produced it -- a
 * `seq` that is not a counter, a client time that came out `NaN` -- would stay invisible.
 */
function checkFrame(frame: AudioFrame, major: number, minor: number): void {
  if (!Number.isInteger(frame.seq) || frame.seq < 0 || frame.seq > U32_MAX) {
    throw new AudioFrameError(`seq ${frame.seq} does not fit in u32`);
  }
  if (!Number.isInteger(frame.clientTimeMs) || frame.clientTimeMs < 0) {
    throw new AudioFrameError(`client time ${frame.clientTimeMs} ms is not a whole number of ms`);
  }
  if (BigInt(frame.clientTimeMs) > U64_MAX) {
    throw new AudioFrameError(`client time ${frame.clientTimeMs} ms does not fit in u64`);
  }
  if (frame.pcm.byteLength % PCM_SAMPLE_WIDTH_BYTES !== 0) {
    throw new AudioFrameError(
      `a PCM16 payload of ${frame.pcm.byteLength} bytes is not a whole number of samples`,
    );
  }
  if (major > U8_MAX || minor > U8_MAX) {
    throw new AudioFrameError(`protocol version ${major}.${minor} does not fit in u8.u8`);
  }
}

/**
 * Float32 samples in [-1, 1] as the payload's PCM16 little-endian bytes. A sample outside the range
 * is clipped, which is what a desk slapped next to the microphone should cost: one distorted frame
 * instead of a signal that wrapped round to the other extreme. A `NaN`, which a device that has
 * gone quiet can produce, becomes the silence `setInt16` writes for it.
 */
export function pcm16Bytes(samples: ArrayLike<number>): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(new ArrayBuffer(samples.length * PCM_SAMPLE_WIDTH_BYTES));
  const view = new DataView(bytes.buffer);
  for (let index = 0; index < samples.length; index += 1) {
    view.setInt16(index * PCM_SAMPLE_WIDTH_BYTES, pcm16Sample(samples[index]), true);
  }
  return bytes;
}

function pcm16Sample(value: number): number {
  const clipped = value < -1 ? -1 : value > 1 ? 1 : value;
  return Math.round(clipped * (clipped < 0 ? 0x8000 : 0x7fff));
}

/**
 * The mono signal of the channels one render quantum carries: their average, which is what the two
 * capsules of a stereo microphone should become. Frames are mono, and the worklet does this before
 * it posts, so the main thread never sees more than one channel.
 */
export function downmixToMono(channels: ArrayLike<ArrayLike<number>>): Float32Array {
  const length = channels.length > 0 ? channels[0].length : 0;
  const mono = new Float32Array(length);
  for (let index = 0; index < length; index += 1) {
    let sum = 0;
    for (let channel = 0; channel < channels.length; channel += 1) sum += channels[channel][index];
    mono[index] = sum / channels.length;
  }
  return mono;
}

/**
 * Float32 mono audio at one rate, resampled to another chunk after chunk.
 *
 * The read position is fractional and a chunk rarely ends on one, so this keeps the position and
 * the input sample an interpolation still needs for the next `push()`: what a chunk's tail cannot
 * settle yet is held back rather than dropped. Resampling every chunk from zero instead would lose
 * that remainder each time, and at 48 kHz -> 16 kHz a 128-sample quantum is 42.67 output samples,
 * so a stateless resampler would throw away 1.6% of a session and pull the frames' client times
 * away from the voice they carry.
 *
 * The interpolation is linear, not a windowed filter: aliasing from above 8 kHz costs a speech
 * recognizer less than filtering would cost inside a render quantum's own budget.
 */
export class AudioResampler {
  /** Input samples per output sample. */
  private readonly step: number;
  /** The input samples no output has consumed yet; at most two, the pair an interpolation spans. */
  private carry = new Float32Array(0);
  /** How many samples this resampler has produced, and how many of the input it has consumed. */
  private produced = 0;
  private consumed = 0;

  constructor(
    /** The rate the audio arrives at, which the `AudioContext` reports. */
    readonly fromRate: number,
    /** The rate a frame's payload carries. */
    readonly toRate: number = PCM_SAMPLE_RATE_HZ,
  ) {
    this.step = fromRate / toRate;
  }

  /**
   * Resamples one chunk, holding back whatever its tail cannot settle yet for the chunk that
   * follows; an empty result means this chunk only fed the hold-back.
   */
  push(chunk: ArrayLike<number>): Float32Array {
    this.append(chunk);
    return this.interpolate();
  }

  private append(chunk: ArrayLike<number>): void {
    if (chunk.length === 0) return;
    if (this.carry.length === 0) {
      this.carry = Float32Array.from(chunk);
      return;
    }
    const joined = new Float32Array(this.carry.length + chunk.length);
    joined.set(this.carry);
    joined.set(chunk, this.carry.length);
    this.carry = joined;
  }

  /**
   * Where output sample `index` falls inside `carry`. It comes from the two counts rather than from
   * a cursor added to each time, so nothing accumulates and the same audio resamples to the same
   * samples whether it arrived in one piece or in a hundred chunks.
   */
  private positionOf(index: number): number {
    return index * this.step - this.consumed;
  }

  /** Every output sample whose position, and the input sample after it, are both in `carry`. */
  private interpolate(): Float32Array {
    const last = this.carry.length - 2;
    const start = this.positionOf(this.produced);
    if (start > last) return new Float32Array(0);
    const count = Math.floor((last - start) / this.step) + 1;
    const output = new Float32Array(count);
    for (let index = 0; index < count; index += 1) {
      const position = this.positionOf(this.produced + index);
      const lower = Math.floor(position);
      const fraction = position - lower;
      const first = this.carry[lower];
      output[index] = first + (this.carry[lower + 1] - first) * fraction;
    }
    this.produced += count;
    // The read position runs past the chunk whenever the sample an interpolation still needs is in
    // the next one, and only whole samples can be dropped, so this is what the chunk held at most.
    const settled = Math.min(Math.floor(this.positionOf(this.produced)), this.carry.length);
    if (settled > 0) {
      this.carry = this.carry.slice(settled);
      this.consumed += settled;
    }
    return output;
  }
}
