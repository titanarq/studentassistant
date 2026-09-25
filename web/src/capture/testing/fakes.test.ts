import { afterEach, describe, expect, it } from "vitest";
import {
  FakeAudioContext,
  FakeAudioWorkletNode,
  FakeImageCapture,
  FakeMediaStream,
  FakeMediaStreamTrack,
  FakeSpeechRecognition,
  FakeWebSocket,
  installAudioFakes,
  installCaptureFakes,
  installMediaFakes,
  installSpeechRecognitionFake,
  installWebSocketFake,
  readGlobal,
} from "./index";
import type {
  FakeSpeechErrorEvent,
  FakeSpeechResultEvent,
  MediaFakes,
} from "./index";

const restores: Array<() => void> = [];

afterEach(() => {
  while (restores.length > 0) restores.pop()?.();
});

function track<T extends { restore(): void }>(fakes: T): T {
  restores.push(() => fakes.restore());
  return fakes;
}

/** Builds a fake the way the code under test does: through the global slot it looks up. */
function construct<T>(globalName: string, ...args: unknown[]): T {
  const Constructor = readGlobal(globalName) as new (...args: unknown[]) => T;
  return new Constructor(...args);
}

/** Runs `action` and returns what it threw, so a test can assert on a `DOMException` name. */
async function thrownBy(action: () => unknown): Promise<unknown> {
  try {
    await action();
  } catch (error) {
    return error;
  }
  return null;
}

describe("media fakes", () => {
  it("puts itself on navigator.mediaDevices and takes it off again", () => {
    const before = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
    const media = installMediaFakes();

    expect(Object.getOwnPropertyDescriptor(navigator, "mediaDevices")?.value).toBe(media.devices);
    media.restore();

    expect(Object.getOwnPropertyDescriptor(navigator, "mediaDevices")).toEqual(before);
  });

  it("hands out a camera and a microphone with the settings a test chose", async () => {
    const media = track(installMediaFakes({ video: { width: 4032, height: 3024 } }));

    const stream = await media.devices.getUserMedia({ video: true, audio: true });

    expect(media.devices.getUserMediaCalls).toEqual([{ video: true, audio: true }]);
    expect(media.devices.streams).toHaveLength(1);
    expect(stream.getVideoTracks()).toHaveLength(1);
    expect(stream.getAudioTracks()).toHaveLength(1);
    expect(media.videoTrack.getSettings()).toMatchObject({
      deviceId: "fake-camera",
      width: 4032,
      height: 3024,
      frameRate: 30,
    });
    expect(media.audioTrack.getSettings()).toMatchObject({
      deviceId: "fake-microphone",
      channelCount: 1,
      sampleRate: 48_000,
    });
  });

  it("hands out only the tracks the constraints asked for", async () => {
    const media = track(installMediaFakes());

    await media.devices.getUserMedia({ video: true });
    await media.devices.getUserMedia({ audio: { echoCancellation: false } });

    expect(media.devices.streams[0].getTracks()).toEqual([media.videoTrack]);
    expect(media.devices.streams[1].getTracks()).toEqual([media.audioTrack]);
  });

  it("rejects getUserMedia with the DOMException name a test set", async () => {
    const media = track(installMediaFakes());
    media.devices.failure = "NotAllowedError";

    const error = await thrownBy(() => media.devices.getUserMedia({ video: true }));

    expect((error as DOMException).name).toBe("NotAllowedError");
    expect(media.devices.streams).toEqual([]);
  });

  it("a track ends when the code stops it and when the device goes away", () => {
    const media = track(installMediaFakes());
    let ended = 0;
    media.videoTrack.onended = () => {
      ended += 1;
    };

    media.videoTrack.stop();

    expect(media.videoTrack.readyState).toBe("ended");
    expect(media.videoTrack.stopCount).toBe(1);
    expect(ended).toBe(1);

    media.videoTrack.stop();
    expect(ended).toBe(1);

    let audioEnded = 0;
    media.audioTrack.addEventListener("ended", () => {
      audioEnded += 1;
    });
    media.audioTrack.endFromDevice();
    expect(media.audioTrack.readyState).toBe("ended");
    expect(media.audioTrack.stopCount).toBe(0);
    expect(audioEnded).toBe(1);
  });

  it("a track reports the constraints it was given and the settings a test changes", async () => {
    const media = track(installMediaFakes());

    await media.videoTrack.applyConstraints({ width: { ideal: 1920 } });
    media.videoTrack.setSettings({ width: 1920, height: 1080 });

    expect(media.videoTrack.appliedConstraints).toEqual([{ width: { ideal: 1920 } }]);
    expect(media.videoTrack.getConstraints()).toEqual({ width: { ideal: 1920 } });
    expect(media.videoTrack.getSettings()).toMatchObject({ width: 1920, height: 1080 });
    expect(media.videoTrack.enabled).toBe(true);
    media.videoTrack.enabled = false;
    expect(media.videoTrack.enabled).toBe(false);
  });

  it("a stream knows its tracks", () => {
    const video = new FakeMediaStreamTrack("video", { width: 800, height: 600 });
    const audio = new FakeMediaStreamTrack("audio");
    const stream = new FakeMediaStream([video]);

    expect(stream.getVideoTracks()).toEqual([video]);
    expect(stream.getAudioTracks()).toEqual([]);
    expect(stream.active).toBe(true);

    stream.addTrack(audio);
    stream.addTrack(audio);
    expect(stream.getTracks()).toEqual([video, audio]);
    expect(stream.getTrackById(audio.id)).toBe(audio);
    expect(stream.getTrackById("no-such-track")).toBeNull();

    stream.removeTrack(audio);
    expect(stream.getTracks()).toEqual([video]);
  });

  it("ImageCapture takes a still at the full resolution the track reports", async () => {
    const media = track(installMediaFakes({ video: { width: 1920, height: 1080 } }));
    expect(readGlobal("ImageCapture")).toBe(FakeImageCapture);

    const capture = construct<FakeImageCapture>("ImageCapture", media.videoTrack);
    const still = await capture.takePhoto();

    expect(media.photos.captures).toEqual([capture]);
    expect(capture.track).toBe(media.videoTrack);
    expect(still.type).toBe("image/png");
    expect(await still.text()).toBe("fake-still-1920x1080");
    expect(media.photos.taken).toEqual([{ settings: undefined, widthPx: 1920, heightPx: 1080 }]);
    expect(await capture.getPhotoCapabilities()).toEqual({
      imageWidth: { min: 1, max: 1920 },
      imageHeight: { min: 1, max: 1080 },
      fillLightMode: ["none"],
      redEyeReduction: "never",
    });
    expect(await capture.getPhotoSettings()).toEqual({ imageWidth: 1920, imageHeight: 1080 });
  });

  it("ImageCapture hands out the blobs a test queued and fails with the name it set", async () => {
    const media = track(installMediaFakes());
    const capture = construct<FakeImageCapture>("ImageCapture", media.videoTrack);
    const queued = new Blob(["queued"], { type: "image/jpeg" });
    media.photos.queued.push(queued);

    expect(await capture.takePhoto({ imageWidth: 640, imageHeight: 480 })).toBe(queued);
    expect(media.photos.taken[0]).toEqual({
      settings: { imageWidth: 640, imageHeight: 480 },
      widthPx: 640,
      heightPx: 480,
    });

    media.photos.failure = "UnknownError";
    const error = await thrownBy(() => capture.takePhoto());
    expect((error as DOMException).name).toBe("UnknownError");
  });

  it("leaves ImageCapture out for a browser without it", () => {
    track(installMediaFakes({ imageCapture: false }));

    expect(readGlobal("ImageCapture")).toBeUndefined();
  });
});

describe("speech recognition fake", () => {
  /** Stands in for the transcriber: finds the API by name, configures it and starts listening. */
  function startListeningLikeTheTranscriber(): FakeSpeechRecognition {
    const found = readGlobal("SpeechRecognition") ?? readGlobal("webkitSpeechRecognition");
    const recognition = new (found as new () => FakeSpeechRecognition)();
    recognition.lang = "es-ES";
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.start();
    return recognition;
  }

  it("is found on both globals and records what the code configured", () => {
    const speech = track(installSpeechRecognitionFake());
    expect(readGlobal("SpeechRecognition")).toBe(FakeSpeechRecognition);
    expect(readGlobal("webkitSpeechRecognition")).toBe(FakeSpeechRecognition);

    const recognition = startListeningLikeTheTranscriber();

    expect(speech.recognitions).toEqual([recognition]);
    expect(recognition.lang).toBe("es-ES");
    expect(recognition.continuous).toBe(true);
    expect(recognition.interimResults).toBe(true);
    expect(recognition.startCount).toBe(1);
    recognition.stop();
    recognition.abort();
    expect(recognition.stopCount).toBe(1);
    expect(recognition.abortCount).toBe(1);
  });

  it("delivers the transcripts a test emits, with their result index and finality", () => {
    track(installSpeechRecognitionFake());
    const recognition = startListeningLikeTheTranscriber();
    const events: FakeSpeechResultEvent[] = [];
    recognition.onresult = (event) => events.push(event);

    recognition.emitResult(
      [
        { transcript: "la célula" },
        { transcript: "la célula tiene membrana", final: true, confidence: 0.75 },
      ],
      3,
    );

    expect(events).toHaveLength(1);
    const event = events[0];
    expect(event.type).toBe("result");
    expect(event.resultIndex).toBe(3);
    expect(event.results).toHaveLength(2);
    expect(event.results[0].isFinal).toBe(false);
    expect(event.results[0][0].transcript).toBe("la célula");
    expect(event.results[0].item(0).confidence).toBe(0);
    expect(event.results[1].isFinal).toBe(true);
    expect(event.results[1][0].transcript).toBe("la célula tiene membrana");
    expect(event.results[1][0].confidence).toBe(0.75);
    expect(event.results.item(1)).toBe(event.results[1]);
  });

  it("delivers start, end and error events to a handler and to a listener", () => {
    track(installSpeechRecognitionFake());
    const recognition = startListeningLikeTheTranscriber();
    const seen: string[] = [];
    recognition.onstart = () => seen.push("start");
    recognition.addEventListener("end", () => seen.push("end"));
    recognition.addEventListener("error", (event: FakeSpeechErrorEvent) =>
      seen.push(`error:${event.error}`),
    );

    recognition.emitStart();
    recognition.emitEnd();
    recognition.emitError("no-speech", "silencio");

    expect(seen).toEqual(["start", "end", "error:no-speech"]);
    expect(recognition.listenerCount("end")).toBe(1);
  });

  it("honours { once: true } and removeEventListener", () => {
    track(installSpeechRecognitionFake());
    const recognition = startListeningLikeTheTranscriber();
    let ends = 0;
    const onEnd = () => {
      ends += 1;
    };
    recognition.addEventListener("end", onEnd, { once: true });

    recognition.emitEnd();
    recognition.emitEnd();

    expect(ends).toBe(1);
    expect(recognition.listenerCount("end")).toBe(0);

    recognition.addEventListener("end", onEnd);
    recognition.removeEventListener("end", onEnd);
    recognition.emitEnd();
    expect(ends).toBe(1);
  });

  it("can be installed webkit-only, or not at all, for a browser without the API", () => {
    const webkit = installSpeechRecognitionFake(["webkitSpeechRecognition"]);
    expect(readGlobal("SpeechRecognition")).toBeUndefined();
    expect(readGlobal("webkitSpeechRecognition")).toBe(FakeSpeechRecognition);
    webkit.restore();

    const none = installSpeechRecognitionFake([]);
    expect(readGlobal("webkitSpeechRecognition")).toBeUndefined();
    none.restore();

    expect(readGlobal("SpeechRecognition")).toBeUndefined();
  });
});

describe("WebSocket fake", () => {
  it("replaces the global socket and starts connecting, recording the url", () => {
    const sockets = track(installWebSocketFake());
    expect(readGlobal("WebSocket")).toBe(FakeWebSocket);

    const socket = new WebSocket("/ws/sessions/42");

    expect(sockets.sockets).toHaveLength(1);
    expect(sockets.sockets[0].url).toBe("/ws/sessions/42");
    expect(sockets.sockets[0].readyState).toBe(FakeWebSocket.CONNECTING);
    expect(socket).toBe(sockets.sockets[0] as unknown as WebSocket);
  });

  it("buffers what is sent before the handshake and flushes it when the server opens", () => {
    const sockets = track(installWebSocketFake());
    const socket = new WebSocket("/ws/sessions/42", "capture.v1");
    socket.send("early");
    expect(sockets.sockets[0].sent).toEqual([]);

    let opened = false;
    socket.onopen = () => {
      opened = true;
    };
    sockets.sockets[0].serverOpen("capture.v1");

    expect(opened).toBe(true);
    expect(sockets.sockets[0].readyState).toBe(FakeWebSocket.OPEN);
    expect(sockets.sockets[0].requestedProtocols).toBe("capture.v1");
    expect(sockets.sockets[0].protocol).toBe("capture.v1");
    expect(sockets.sockets[0].sentText).toEqual(["early"]);

    sockets.sockets[0].serverOpen();
    expect(sockets.sockets[0].readyState).toBe(FakeWebSocket.OPEN);
  });

  it("records text frames and copies the bytes of binary frames", () => {
    const sockets = track(installWebSocketFake());
    const socket = new WebSocket("/ws/sessions/42");
    const fake = sockets.sockets[0];
    fake.serverOpen();

    socket.send('{"type":"hello"}');
    socket.send(new Uint8Array([83, 65, 65, 70]).buffer);
    socket.send(new Uint8Array([1, 2]));
    socket.send(new DataView(new Uint8Array([3, 4]).buffer));

    expect(fake.sentText).toEqual(['{"type":"hello"}']);
    expect(fake.sent[0]).toEqual({ kind: "text", text: '{"type":"hello"}' });
    expect(fake.sentBinary.map((bytes) => [...bytes])).toEqual([
      [83, 65, 65, 70],
      [1, 2],
      [3, 4],
    ]);

    const reused = new Uint8Array([9, 9]);
    socket.send(reused);
    reused[0] = 0;
    expect([...fake.sentBinary[fake.sentBinary.length - 1]]).toEqual([9, 9]);
  });

  it("refuses a Blob frame, which the capture client never sends", () => {
    const sockets = track(installWebSocketFake());
    const socket = new WebSocket("/ws/sessions/42");
    sockets.sockets[0].serverOpen();

    expect(() => socket.send(new Blob(["no"]))).toThrow(/Blob/);
  });

  it("delivers server text and bytes in the binaryType the code asked for", async () => {
    const sockets = track(installWebSocketFake());
    const socket = new WebSocket("/ws/sessions/42");
    const fake = sockets.sockets[0];
    const received: unknown[] = [];
    socket.addEventListener("message", (event: MessageEvent) => received.push(event.data));
    fake.serverOpen();

    fake.serverMessage('{"type":"transcript.final"}');
    fake.serverMessage(new Uint8Array([83, 65, 65, 70]));

    expect(received[0]).toBe('{"type":"transcript.final"}');
    expect(received[1]).toBeInstanceOf(Blob);
    expect(await (received[1] as Blob).text()).toBe("SAAF");

    socket.binaryType = "arraybuffer";
    fake.serverMessage(new Uint8Array([1, 2]).buffer);

    expect(received[2]).toBeInstanceOf(ArrayBuffer);
    expect([...new Uint8Array(received[2] as ArrayBuffer)]).toEqual([1, 2]);
    expect(fake.binaryType).toBe("arraybuffer");
  });

  it("refuses a server message pushed before the socket is open", () => {
    const sockets = track(installWebSocketFake());
    new WebSocket("/ws/sessions/42");

    expect(() => sockets.sockets[0].serverMessage("too early")).toThrow(/only while/);
  });

  it("reports the server closing, cleanly and as a lost connection", () => {
    const sockets = track(installWebSocketFake());
    const socket = new WebSocket("/ws/sessions/42");
    const fake = sockets.sockets[0];
    const closes: CloseEvent[] = [];
    socket.onclose = (event) => closes.push(event);
    fake.serverOpen();

    fake.serverClose(1001, "sesión terminada");

    expect(fake.readyState).toBe(FakeWebSocket.CLOSED);
    expect(closes[0].code).toBe(1001);
    expect(closes[0].reason).toBe("sesión terminada");
    expect(closes[0].wasClean).toBe(true);

    const lost = new WebSocket("/ws/sessions/43");
    lost.onclose = (event) => closes.push(event);
    const lostFake = sockets.sockets[1];
    lostFake.serverOpen();

    lostFake.serverClose();

    expect(lostFake.readyState).toBe(FakeWebSocket.CLOSED);
    expect(lost).toBe(lostFake as unknown as WebSocket);
    expect(closes[1].code).toBe(1006);
    expect(closes[1].wasClean).toBe(false);
  });

  it("reports a connection error and records what the code closed with", () => {
    const sockets = track(installWebSocketFake());
    const socket = new WebSocket("/ws/sessions/42");
    const fake = sockets.sockets[0];
    const errors: string[] = [];
    socket.onerror = (event) => errors.push((event as ErrorEvent).message);
    fake.serverOpen();

    fake.serverError("se cayó el backend");
    socket.close(1000, "terminar");

    expect(errors).toEqual(["se cayó el backend"]);
    expect(fake.closeCalls).toEqual([{ code: 1000, reason: "terminar" }]);
    expect(fake.readyState).toBe(FakeWebSocket.CLOSED);
    expect(() => socket.send("late")).toThrow(/closing or closed/);
  });
});

describe("audio fakes", () => {
  /** Stands in for the streaming transcriber: builds the graph the browser would give it. */
  async function buildGraphLikeTheTranscriber(media: MediaFakes): Promise<void> {
    const context = new AudioContext({ sampleRate: 16_000 });
    await context.audioWorklet.addModule("/src/capture/pcmWorklet.ts");
    const stream = await media.devices.getUserMedia({ audio: true });
    const source = context.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(context, "pcm-processor", {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      channelCount: 1,
      processorOptions: { targetSampleRate: 16_000 },
    });
    source.connect(node as unknown as AudioNode);
    node.connect(context.destination);
    await context.resume();
  }

  it("replaces the globals and records the graph the code builds", async () => {
    const media = track(installMediaFakes());
    const audio = track(installAudioFakes());
    expect(readGlobal("AudioContext")).toBe(FakeAudioContext);
    expect(readGlobal("AudioWorkletNode")).toBe(FakeAudioWorkletNode);

    await buildGraphLikeTheTranscriber(media);

    const context = audio.contexts[0];
    const node = audio.workletNodes[0];
    expect(context.sampleRate).toBe(16_000);
    expect(context.state).toBe("running");
    expect(context.resumeCount).toBe(1);
    expect(context.audioWorklet.moduleUrls).toEqual(["/src/capture/pcmWorklet.ts"]);
    expect(context.sources).toHaveLength(1);
    expect(context.sources[0].stream as unknown).toBe(media.devices.streams[0]);
    expect(context.sources[0].node.connections).toEqual([node]);
    expect(node.connections).toEqual([context.destination]);
    expect(node.context).toBe(context);
    expect(node.processorName).toBe("pcm-processor");
    expect(node.options).toEqual({
      numberOfInputs: 1,
      numberOfOutputs: 0,
      channelCount: 1,
      processorOptions: { targetSampleRate: 16_000 },
    });
  });

  it("a context starts suspended and reports suspend and close", async () => {
    const audio = track(installAudioFakes());

    const context = new AudioContext();

    expect(audio.contexts[0].sampleRate).toBe(48_000);
    expect(context.state).toBe("suspended");
    await context.resume();
    expect(context.state).toBe("running");
    await context.suspend();
    expect(context.state).toBe("suspended");
    expect(audio.contexts[0].suspendCount).toBe(1);
    await context.close();
    expect(context.state).toBe("closed");
    expect(audio.contexts[0].closeCount).toBe(1);
    await context.resume();
    expect(context.state).toBe("closed");
  });

  it("addModule fails with the reason a test set", async () => {
    const audio = track(installAudioFakes());
    new AudioContext();
    audio.contexts[0].audioWorklet.failure = "el worklet no compila";

    const error = await thrownBy(() => audio.contexts[0].audioWorklet.addModule("/x.ts"));

    expect((error as Error).message).toBe("el worklet no compila");
    expect(audio.contexts[0].audioWorklet.moduleUrls).toEqual([]);
  });

  it("carries chunks from the processor to the main thread and messages back", () => {
    const audio = track(installAudioFakes());
    new AudioContext({ sampleRate: 16_000 });
    const node = new FakeAudioWorkletNode(audio.contexts[0], "pcm-processor");
    const chunks: unknown[] = [];
    node.port.onmessage = (event) => chunks.push(event.data);
    node.port.addEventListener("message", (event: MessageEvent) =>
      chunks.push(`listener:${String(event.data)}`),
    );

    node.emitProcessorMessage(new Float32Array([0.5, -0.5]));

    expect(chunks).toHaveLength(2);
    expect([...(chunks[0] as Float32Array)]).toEqual([0.5, -0.5]);
    expect(chunks[1]).toBe(`listener:${String(new Float32Array([0.5, -0.5]))}`);
    expect(node.port.startCount).toBe(0);

    node.port.postMessage({ command: "flush" });
    node.port.start();
    node.port.close();
    node.disconnect();

    expect(node.port.posted).toEqual([{ command: "flush" }]);
    expect(node.port.startCount).toBe(1);
    expect(node.port.closeCount).toBe(1);
    expect(node.disconnectCount).toBe(1);
  });
});

describe("installCaptureFakes", () => {
  it("installs every fake at once and restores all of them", () => {
    const fakes = track(installCaptureFakes({ media: { video: { width: 640, height: 480 } } }));

    expect(readGlobal("ImageCapture")).toBe(FakeImageCapture);
    expect(readGlobal("SpeechRecognition")).toBe(FakeSpeechRecognition);
    expect(readGlobal("WebSocket")).toBe(FakeWebSocket);
    expect(readGlobal("AudioContext")).toBe(FakeAudioContext);
    expect(readGlobal("AudioWorkletNode")).toBe(FakeAudioWorkletNode);
    expect(fakes.videoTrack.getSettings()).toMatchObject({ width: 640, height: 480 });
    expect(fakes.recognitions).toEqual([]);
    expect(fakes.sockets).toEqual([]);
    expect(fakes.contexts).toEqual([]);
    expect(fakes.workletNodes).toEqual([]);

    fakes.restore();

    expect(readGlobal("WebSocket")).not.toBe(FakeWebSocket);
    expect(readGlobal("AudioContext")).not.toBe(FakeAudioContext);
    expect(readGlobal("SpeechRecognition")).toBeUndefined();
    expect(readGlobal("ImageCapture")).toBeUndefined();
  });

  it("installs no speech recognition for a browser without the API", () => {
    track(installCaptureFakes({ speechGlobals: [] }));

    expect(readGlobal("SpeechRecognition")).toBeUndefined();
    expect(readGlobal("webkitSpeechRecognition")).toBeUndefined();
    expect(readGlobal("WebSocket")).toBe(FakeWebSocket);
  });
});
