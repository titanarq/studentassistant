/**
 * The frame encoder and the resampler on their own (#40). Every expected byte is derived from
 * protocol/README.md's two tables -- the header's, big-endian, and the payload's, PCM16
 * little-endian -- and every expected sample from a signal whose values are exact in Float32, so a
 * frame that goes out in the wrong order or a chunk that loses its tail cannot pass.
 */

import { describe, expect, it } from "vitest";

import { parseVersion, PROTOCOL_VERSION } from "../protocol";
import {
  AUDIO_HEADER_SIZE,
  AUDIO_MAGIC,
  AudioFrameError,
  AudioResampler,
  downmixToMono,
  encodeAudioFrame,
  pcm16Bytes,
  PCM_SAMPLE_RATE_HZ,
  PCM_SAMPLE_WIDTH_BYTES,
} from "./audioFrames";
import { CLIENT_AUDIO_FORMAT } from "./sessionSocket";

/** The capture time the shared examples use, and the eight bytes its u64 field carries. */
const CLIENT_TIME_MS = 1790251212300;
const CLIENT_TIME_BYTES = [0x00, 0x00, 0x01, 0xa0, 0xd3, 0x49, 0x9e, 0x0c];
/** A `seq` whose four bytes differ, so the header cannot pass by being written the other way round. */
const SEQ = 0x01020304;

/** Bytes read back as the little-endian signed 16-bit samples they are. */
function int16(bytes: Uint8Array, offset = 0): number[] {
  const view = new DataView(bytes.buffer, bytes.byteOffset + offset, bytes.byteLength - offset);
  const samples: number[] = [];
  for (let at = 0; at < view.byteLength; at += PCM_SAMPLE_WIDTH_BYTES) {
    samples.push(view.getInt16(at, true));
  }
  return samples;
}

/** One frame of a payload these samples become. */
function encode(samples: ArrayLike<number>): Uint8Array<ArrayBuffer> {
  return encodeAudioFrame({
    seq: SEQ,
    clientTimeMs: CLIENT_TIME_MS,
    pcm: pcm16Bytes(samples),
  });
}

/** A sine whose values a test can name; `length` samples of it at `rate`. */
function sine(rate: number, frequency: number, length: number): Float32Array {
  const signal = new Float32Array(length);
  for (let index = 0; index < length; index += 1) {
    signal[index] = Math.sin((2 * Math.PI * frequency * index) / rate);
  }
  return signal;
}

/** A ramp in eighths, which Float32 holds exactly, so an assertion can name its samples. */
function ramp(length: number): Float32Array {
  const signal = new Float32Array(length);
  for (let index = 0; index < length; index += 1) signal[index] = index / 128;
  return signal;
}

describe("the layout protocol/README.md fixes", () => {
  it("is an 18-byte SAAF header over PCM16 at the rate hello announces", () => {
    expect(AUDIO_MAGIC).toBe("SAAF");
    expect(AUDIO_HEADER_SIZE).toBe(18);
    expect(PCM_SAMPLE_WIDTH_BYTES).toBe(2);
    expect(PCM_SAMPLE_RATE_HZ).toBe(CLIENT_AUDIO_FORMAT.sample_rate_hz);
  });
});

describe("encodeAudioFrame", () => {
  it("writes the header's fields big-endian and the payload's samples little-endian", () => {
    const frame = encode([0.5]);

    expect(frame.byteLength).toBe(AUDIO_HEADER_SIZE + PCM_SAMPLE_WIDTH_BYTES);
    expect([...frame.slice(0, 4)]).toEqual([0x53, 0x41, 0x41, 0x46]);
    expect([...frame.slice(4, 6)]).toEqual(parseVersion(PROTOCOL_VERSION));
    expect([...frame.slice(6, 10)]).toEqual([0x01, 0x02, 0x03, 0x04]);
    expect([...frame.slice(10, 18)]).toEqual(CLIENT_TIME_BYTES);
    // 0.5 of full scale is 16384, which is 0x4000 with its low byte first.
    expect([...frame.slice(AUDIO_HEADER_SIZE)]).toEqual([0x00, 0x40]);
  });

  it("carries the version it is told to, for a backend that negotiated another minor", () => {
    const frame = encodeAudioFrame({
      seq: 0,
      clientTimeMs: 0,
      pcm: pcm16Bytes([0]),
      protocolVersion: "1.0",
    });

    expect([...frame.slice(4, 6)]).toEqual([1, 0]);
  });

  it("counts every sample of the payload and nothing else", () => {
    expect(encode([0.25, -0.25, 0.5]).byteLength).toBe(AUDIO_HEADER_SIZE + 6);
    expect(int16(encode([0.25, -0.25, 0.5]), AUDIO_HEADER_SIZE)).toEqual([8192, -8192, 16384]);
  });

  it("refuses a frame that would go out corrupt", () => {
    const pcm = pcm16Bytes([0]);
    const frame = (fields: Partial<Parameters<typeof encodeAudioFrame>[0]>) =>
      encodeAudioFrame({ seq: 0, clientTimeMs: 0, pcm, ...fields });

    expect(() => frame({ seq: -1 })).toThrow(AudioFrameError);
    expect(() => frame({ seq: 1.5 })).toThrow(/u32/);
    expect(() => frame({ seq: 2 ** 32 })).toThrow(/u32/);
    expect(() => frame({ clientTimeMs: Number.NaN })).toThrow(/ms/);
    expect(() => frame({ clientTimeMs: -1 })).toThrow(/ms/);
    expect(() => frame({ pcm: new Uint8Array(3) })).toThrow(/whole number of samples/);
    expect(() => frame({ protocolVersion: "300.0" })).toThrow(/u8\.u8/);
  });
});

describe("pcm16Bytes", () => {
  it("writes full scale at both ends and silence in the middle", () => {
    expect(int16(pcm16Bytes([1, 0, -1]))).toEqual([32767, 0, -32768]);
  });

  it("clips what reached the microphone too loudly instead of wrapping it round", () => {
    expect(int16(pcm16Bytes([2, -2]))).toEqual([32767, -32768]);
  });

  it("writes silence for a sample that is not a number", () => {
    expect(int16(pcm16Bytes([Number.NaN]))).toEqual([0]);
  });
});

describe("downmixToMono", () => {
  it("averages the channels of a stereo microphone", () => {
    expect([...downmixToMono([[0.25, -0.5], [0.75, 0.5]])]).toEqual([0.5, 0]);
  });

  it("keeps the single channel a mono microphone gives", () => {
    expect([...downmixToMono([[0.25, -0.5]])]).toEqual([0.25, -0.5]);
  });

  it("has nothing to mix when the quantum carries no channel", () => {
    expect(downmixToMono([]).length).toBe(0);
  });
});

describe("AudioResampler", () => {
  it("takes every third sample of 48 kHz audio for the 16 kHz a frame carries", () => {
    const resampled = new AudioResampler(48_000, PCM_SAMPLE_RATE_HZ).push(ramp(9));

    expect([...resampled]).toEqual([0, 3, 6].map((index) => index / 128));
  });

  it("produces the number of samples the two rates imply for the chunk it is given", () => {
    // A tenth of a second: 4800 samples in, 1600 out, and the default rate is the payload's own.
    expect(new AudioResampler(48_000).push(ramp(4800)).length).toBe(1600);
  });

  it("interpolates between the samples of a rate slower than the payload's", () => {
    const resampled = new AudioResampler(8_000).push(new Float32Array([0, 0.5, 1]));

    expect([...resampled]).toEqual([0, 0.25, 0.5]);
  });

  it("keeps the frequency of a known signal", () => {
    // 100 Hz captured at 48 kHz: a period is 160 of the 16 kHz samples a frame carries, so the
    // quarter periods land on output samples 40, 80 and 120 whatever rate the audio arrived at.
    const resampled = new AudioResampler(48_000).push(sine(48_000, 100, 480));

    expect(resampled.length).toBe(160);
    expect(resampled[40]).toBeCloseTo(1, 5);
    expect(resampled[80]).toBeCloseTo(0, 5);
    expect(resampled[120]).toBeCloseTo(-1, 5);
  });

  it("holds a chunk's last sample back for the chunk that follows", () => {
    const resampler = new AudioResampler(PCM_SAMPLE_RATE_HZ);

    expect([...resampler.push(new Float32Array([0.25, 0.5]))]).toEqual([0.25]);
    expect([...resampler.push(new Float32Array([0.75]))]).toEqual([0.5]);
    expect([...resampler.push(new Float32Array([1]))]).toEqual([0.75]);
  });

  it("resamples a stream of quanta exactly as the same audio in one piece", () => {
    const signal = sine(48_000, 440, 4800);

    expect(pushInQuanta(new AudioResampler(48_000), signal)).toEqual([
      ...new AudioResampler(48_000).push(signal),
    ]);
  });

  it("keeps the timeline of a rate that is no whole multiple of the payload's", () => {
    const signal = sine(44_100, 440, 4410);
    const onePiece = new AudioResampler(44_100).push(signal);
    const streamed = pushInQuanta(new AudioResampler(44_100), signal);

    expect(streamed.length).toBe(onePiece.length);
    // A read position is derived from two counts, never added to, so the two runs differ only where
    // a rounding lands on the other side of an input sample -- which is nowhere in a session.
    const widest = streamed.reduce(
      (worst, sample, index) => Math.max(worst, Math.abs(sample - onePiece[index])),
      0,
    );
    expect(widest).toBeLessThan(1e-6);
  });
});

/** What a worklet hands over: the signal in render quanta of 128 samples. */
function pushInQuanta(resampler: AudioResampler, signal: Float32Array): number[] {
  const resampled: number[] = [];
  for (let start = 0; start < signal.length; start += 128) {
    resampled.push(...resampler.push(signal.subarray(start, start + 128)));
  }
  return resampled;
}
