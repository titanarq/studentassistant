import { afterEach, expect, it, vi } from "vitest";
import { installSpeechRecognitionFake, type SpeechFakes } from "../capture/testing";
import { listenForQuestion, type VoiceQuestionCallbacks, voiceQuestionSupported } from "./voiceQuestion";

let fakes: SpeechFakes | null = null;

afterEach(() => {
  fakes?.restore();
  fakes = null;
});

function callbacks() {
  return {
    onInterim: vi.fn(),
    onFinal: vi.fn(),
    onProblem: vi.fn(),
    onEnd: vi.fn(),
  } satisfies VoiceQuestionCallbacks;
}

it("listens for one Spanish utterance and hands over its final text", () => {
  fakes = installSpeechRecognitionFake();
  expect(voiceQuestionSupported()).toBe(true);
  const seen = callbacks();
  const handle = listenForQuestion(seen);
  const [recognition] = fakes.recognitions;
  expect(recognition.lang).toBe("es-ES");
  expect(recognition.continuous).toBe(false);
  expect(recognition.interimResults).toBe(true);
  expect(recognition.startCount).toBe(1);

  recognition.emitResult([{ transcript: "qué era" }]);
  expect(seen.onInterim).toHaveBeenLastCalledWith("qué era");
  recognition.emitResult([{ transcript: "qué era la derivada", final: true }]);
  handle.stop();
  expect(recognition.stopCount).toBe(1);
  recognition.emitEnd();
  expect(seen.onFinal).toHaveBeenCalledWith("qué era la derivada");
  expect(seen.onProblem).not.toHaveBeenCalled();
  expect(seen.onEnd).toHaveBeenCalledTimes(1);
  handle.stop();
  expect(recognition.stopCount).toBe(1);
});

it("reports silence, a denied microphone and a missing API", () => {
  fakes = installSpeechRecognitionFake();
  const silent = callbacks();
  listenForQuestion(silent);
  fakes.recognitions[0].emitEnd();
  expect(silent.onProblem).toHaveBeenCalledWith("no-speech");
  expect(silent.onFinal).not.toHaveBeenCalled();

  const denied = callbacks();
  listenForQuestion(denied);
  fakes.recognitions[1].emitError("not-allowed");
  fakes.recognitions[1].emitEnd();
  expect(denied.onProblem).toHaveBeenCalledWith("permission-denied");

  const odd = callbacks();
  listenForQuestion(odd);
  fakes.recognitions[2].emitError("aborted");
  fakes.recognitions[2].emitError("audio-capture");
  fakes.recognitions[2].emitEnd();
  expect(odd.onProblem).toHaveBeenCalledWith("unavailable");
  fakes.restore();

  fakes = installSpeechRecognitionFake([]);
  expect(voiceQuestionSupported()).toBe(false);
  const none = callbacks();
  listenForQuestion(none);
  expect(none.onProblem).toHaveBeenCalledWith("unsupported");
  expect(none.onEnd).toHaveBeenCalled();
});
