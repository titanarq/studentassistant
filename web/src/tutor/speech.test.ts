import { afterEach, expect, it, vi } from "vitest";
import { browserSpeechOutput, shownText, spokenText } from "./speech";

afterEach(() => {
  vi.unstubAllGlobals();
});

it("reads the answer without footnote marks, doubts or Markdown", () => {
  expect(spokenText("La **derivada** es un límite.[^p1][^t1] Se escribe `f'(x)` [[?1791]].[^p2]")).toBe(
    "La derivada es un límite. Se escribe f'(x) 1791.",
  );
  expect(spokenText("  \n")).toBe("");
});

it("shows each footnote mark as its label", () => {
  expect(shownText("Un límite.[^p1][^t1]")).toBe("Un límite.[p1][t1]");
});

it("speaks in Spanish with a Spanish voice, one text at a time", () => {
  const spoken: { text: string; lang: string; voice: unknown; onend: (() => void) | null }[] = [];
  class Utterance {
    lang = "";
    voice: unknown = undefined;
    onend: (() => void) | null = null;
    onerror: (() => void) | null = null;
    constructor(readonly text: string) {}
  }
  const synthesis = {
    speak: vi.fn((utterance: Utterance) => spoken.push(utterance)),
    cancel: vi.fn(),
    getVoices: () => [{ lang: "en-US" }, { lang: "es-MX" }, { lang: "es-ES" }],
  };
  vi.stubGlobal("speechSynthesis", synthesis);
  vi.stubGlobal("SpeechSynthesisUtterance", Utterance);

  const output = browserSpeechOutput();
  expect(output.supported).toBe(true);
  const onEnd = vi.fn();
  output.speak("Hola", onEnd);
  expect(synthesis.cancel).toHaveBeenCalledTimes(1);
  expect(spoken[0].text).toBe("Hola");
  expect(spoken[0].lang).toBe("es-ES");
  expect(spoken[0].voice).toEqual({ lang: "es-ES" });
  spoken[0].onend?.();
  spoken[0].onend?.();
  expect(onEnd).toHaveBeenCalledTimes(1);
  output.cancel();
  expect(synthesis.cancel).toHaveBeenCalledTimes(2);
});

it("is unsupported without speechSynthesis", () => {
  vi.stubGlobal("speechSynthesis", undefined);
  const output = browserSpeechOutput();
  expect(output.supported).toBe(false);
  const onEnd = vi.fn();
  output.speak("Hola", onEnd);
  expect(onEnd).toHaveBeenCalled();
});
