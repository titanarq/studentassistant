import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import ChatComposer from "./ChatComposer";

const props = {
  id: "composer",
  label: "Mensaje",
  placeholder: "Escribe",
  onChange: () => undefined,
  onSubmit: () => undefined,
  canSubmit: true,
  maxLength: 1000,
};

it("keeps the end of text written while unfocused (dictation) in view", () => {
  const { rerender } = render(<ChatComposer {...props} value="" />);
  const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
  Object.defineProperty(textarea, "scrollHeight", { configurable: true, value: 480 });
  rerender(<ChatComposer {...props} value={"una frase larga dictada ".repeat(40)} />);
  expect(textarea.scrollTop).toBe(480);
});

it("leaves the scroll alone while the student types", () => {
  const { rerender } = render(<ChatComposer {...props} value="" />);
  const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
  textarea.focus();
  Object.defineProperty(textarea, "scrollHeight", { configurable: true, value: 480 });
  rerender(<ChatComposer {...props} value="escribo" />);
  expect(textarea.scrollTop).toBe(0);
});

it("still hands the textarea to the caller's ref", () => {
  const ref = { current: null as HTMLTextAreaElement | null };
  render(<ChatComposer {...props} value="" inputRef={ref} />);
  expect(ref.current).toBe(screen.getByRole("textbox"));
});
