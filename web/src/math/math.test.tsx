import { render } from "@testing-library/react";
import { expect, it } from "vitest";
import { parseInline, parseNotes } from "../notes/markdown";
import NotesView from "../notes/NotesView";
import ReplyView from "../study/chat/ReplyView";
import { MathText } from "./Math";

const none = () => null;

it("reads inline and display formulas in a paragraph", () => {
  expect(parseInline("La derivada $f'(x)$ y $$x^2$$ y \\$5")).toEqual([
    { type: "text", text: "La derivada " },
    { type: "math", text: "f'(x)", display: false },
    { type: "text", text: " y " },
    { type: "math", text: "x^2", display: true },
    { type: "text", text: " y $5" },
  ]);
});

it("does not take prices or lone dollar signs for a formula", () => {
  expect(parseInline("Cuesta 5$ y luego 6$.")).toEqual([{ type: "text", text: "Cuesta 5$ y luego 6$." }]);
  expect(parseInline("un $ solo")).toEqual([{ type: "text", text: "un $ solo" }]);
});

it("keeps markdown characters inside a formula as they are", () => {
  expect(parseInline("$a_i * b_i$ *cursiva*")).toEqual([
    { type: "math", text: "a_i * b_i", display: false },
    { type: "text", text: " " },
    { type: "em", children: [{ type: "text", text: "cursiva" }] },
  ]);
});

it("reads a display formula on its own lines as a block", () => {
  const tree = parseNotes("Antes\n\n$$\n\\int_0^1 x\\,dx\n$$\n\n$$y = 2$$\n\nDespués\n");
  expect(tree.blocks).toEqual([
    { type: "paragraph", text: "Antes" },
    { type: "math", text: "\\int_0^1 x\\,dx" },
    { type: "math", text: "y = 2" },
    { type: "paragraph", text: "Después" },
  ]);
});

it("renders inline and display formulas in the notes with KaTeX", () => {
  const { container } = render(
    <NotesView tree={parseNotes("## Cálculo\n\nLa derivada de $x^2$ es $2x$.\n\n$$\\frac{1}{2}$$\n")} onOpenSource={none} />,
  );
  expect(container.querySelectorAll("p .math .katex")).toHaveLength(2);
  expect(container.querySelector(".math-display .katex-display")).not.toBeNull();
  // The raw delimiters are not shown.
  expect(container.textContent).not.toContain("$");
});

it("a malformed formula shows its source and does not break the page", () => {
  const { container } = render(
    <NotesView tree={parseNotes("## Cálculo\n\nRota: $\\frac{1}{$ y buena: $x^2$.\n\n$$\\undefinedmacro{$$\n")} onOpenSource={none} />,
  );
  const errors = container.querySelectorAll(".math-error");
  expect(errors).toHaveLength(2);
  expect(errors[0]).toHaveTextContent("$\\frac{1}{$");
  expect(errors[1]).toHaveTextContent("$$\\undefinedmacro{$$");
  expect(container.querySelectorAll(".math .katex")).toHaveLength(1);
});

it("renders the formulas of a written answer of the study chat", () => {
  const { container } = render(
    <ReplyView text={"La **derivada** es $f'(x)$.\n\n$$x^2$$"} renderSection={none} renderSource={none} />,
  );
  expect(container.querySelectorAll(".math .katex")).toHaveLength(2);
  expect(container.querySelector("strong")).toHaveTextContent("derivada");
});

it("renders the formulas of a plain-text bubble and leaves the rest as text", () => {
  const { container } = render(
    <p>
      <MathText text="Aquí $a^2 + b^2 = c^2$ y $\frac{1}{$ y 5$ y 6$" />
    </p>,
  );
  expect(container.querySelectorAll(".math .katex")).toHaveLength(1);
  expect(container.querySelectorAll(".math-error")).toHaveLength(1);
  expect(container.textContent).toContain("5$ y 6$");
});

it("shows text without formulas unchanged", () => {
  const { container } = render(
    <p>
      <MathText text="Sin fórmulas." />
    </p>,
  );
  expect(container.textContent).toBe("Sin fórmulas.");
});
