import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { installSpeechRecognitionFake } from "../capture/testing/speech";
import VoiceInputButton, { CHAT_VOICE_PROBLEMS } from "./VoiceInputButton";
import type { VoiceQuestionCallbacks, VoiceQuestionStarter } from "./voiceQuestion";

function fakeListen() {
  const calls: VoiceQuestionCallbacks[] = [];
  const stop = vi.fn();
  const listen: VoiceQuestionStarter = (callbacks) => {
    calls.push(callbacks);
    return { stop };
  };
  return { listen, calls, stop };
}

function Harness(props: { listen?: VoiceQuestionStarter; voiceSupported?: boolean; onFinal?: (text: string) => void; disabled?: boolean }) {
  const [draft, setDraft] = useState("");
  return (
    <form>
      <label htmlFor="input">Mensaje</label>
      <input id="input" value={draft} onChange={(event) => setDraft(event.target.value)} />
      <VoiceInputButton
        value={draft}
        onChange={setDraft}
        onFinal={(text) => {
          setDraft(text);
          props.onFinal?.(text);
        }}
        disabled={props.disabled}
        listen={props.listen}
        voiceSupported={props.voiceSupported}
      />
    </form>
  );
}

const input = () => screen.getByLabelText("Mensaje") as HTMLInputElement;

describe("VoiceInputButton", () => {
  let uninstall: (() => void) | null = null;
  afterEach(() => {
    uninstall?.();
    uninstall = null;
  });

  it("shows the interim text in the input and hands over the final text once", () => {
    const { listen, calls } = fakeListen();
    const onFinal = vi.fn();
    render(<Harness listen={listen} voiceSupported onFinal={onFinal} />);
    const button = screen.getByRole("button", { name: "Dictar el mensaje por voz" });
    expect(button).toHaveTextContent("Hablar");
    expect(button).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(button);
    expect(screen.getByRole("button", { name: "Escuchando… (pulsa para parar)" })).toHaveAttribute("aria-pressed", "true");
    act(() => calls[0].onInterim?.("pon un"));
    expect(input().value).toBe("pon un");
    act(() => {
      calls[0].onFinal("pon un ejemplo");
      calls[0].onEnd?.();
    });
    expect(input().value).toBe("pon un ejemplo");
    expect(onFinal).toHaveBeenCalledTimes(1);
    expect(onFinal).toHaveBeenCalledWith("pon un ejemplo");
    expect(screen.getByRole("button", { name: "Dictar el mensaje por voz" })).toHaveAttribute("aria-pressed", "false");
  });

  it("stops the recognition when pressed again", () => {
    const { listen, calls, stop } = fakeListen();
    render(<Harness listen={listen} voiceSupported />);
    fireEvent.click(screen.getByRole("button", { name: "Dictar el mensaje por voz" }));
    fireEvent.click(screen.getByRole("button", { name: "Escuchando… (pulsa para parar)" }));
    expect(stop).toHaveBeenCalledTimes(1);
    expect(calls).toHaveLength(1);
  });

  it("shows a problem's line, restores the text from before and clears the line when the student types", () => {
    const { listen, calls } = fakeListen();
    render(<Harness listen={listen} voiceSupported />);
    fireEvent.change(input(), { target: { value: "hola" } });
    fireEvent.click(screen.getByRole("button", { name: "Dictar el mensaje por voz" }));
    act(() => calls[0].onInterim?.("algo"));
    act(() => {
      calls[0].onProblem("no-speech");
      calls[0].onEnd?.();
    });
    expect(input().value).toBe("hola");
    expect(screen.getByRole("alert")).toHaveTextContent("No te he oído. Pulsa «Hablar» y habla.");
    fireEvent.change(input(), { target: { value: "hola!" } });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("clears a problem's line on the next press", () => {
    const { listen, calls } = fakeListen();
    render(<Harness listen={listen} voiceSupported />);
    fireEvent.click(screen.getByRole("button", { name: "Dictar el mensaje por voz" }));
    act(() => {
      calls[0].onProblem("permission-denied");
      calls[0].onEnd?.();
    });
    expect(screen.getByRole("alert")).toHaveTextContent(CHAT_VOICE_PROBLEMS["permission-denied"]);
    fireEvent.click(screen.getByRole("button", { name: "Dictar el mensaje por voz" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("is disabled with a hint in a browser without speech recognition", () => {
    render(<Harness voiceSupported={false} />);
    const button = screen.getByRole("button", { name: "Dictar el mensaje por voz" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "Este navegador no reconoce la voz: escribe tu mensaje.");
    expect(button).toHaveAccessibleDescription("Este navegador no reconoce la voz: escribe tu mensaje.");
    fireEvent.change(input(), { target: { value: "escribo" } });
    expect(input().value).toBe("escribo");
  });

  it("stops a running recognition on unmount and ignores what comes after", () => {
    const { listen, calls, stop } = fakeListen();
    const onFinal = vi.fn();
    const { unmount } = render(<Harness listen={listen} voiceSupported onFinal={onFinal} />);
    fireEvent.click(screen.getByRole("button", { name: "Dictar el mensaje por voz" }));
    unmount();
    expect(stop).toHaveBeenCalledTimes(1);
    calls[0].onFinal("tarde");
    calls[0].onEnd?.();
    expect(onFinal).not.toHaveBeenCalled();
  });

  it("stops a running recognition when it becomes disabled", () => {
    const { listen, stop } = fakeListen();
    const { rerender } = render(<Harness listen={listen} voiceSupported />);
    fireEvent.click(screen.getByRole("button", { name: "Dictar el mensaje por voz" }));
    rerender(<Harness listen={listen} voiceSupported disabled />);
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it("drives the browser's recognition by default", () => {
    const fake = installSpeechRecognitionFake();
    uninstall = fake.restore;
    const onFinal = vi.fn();
    render(<Harness onFinal={onFinal} />);
    fireEvent.click(screen.getByRole("button", { name: "Dictar el mensaje por voz" }));
    const recognition = fake.recognitions[0];
    expect(recognition.lang).toBe("es-ES");
    act(() => recognition.emitResult([{ transcript: "hola", final: true }]));
    act(() => recognition.emitEnd());
    expect(onFinal).toHaveBeenCalledWith("hola");
  });
});
