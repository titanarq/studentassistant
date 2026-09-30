/**
 * LaTeX in the visual editor (#532): `$x^2$` and `$$ ... $$` (remark-math) as two atomic nodes
 * that show the formula rendered with KaTeX. `@milkdown/plugin-math` is not used: its release is
 * from Milkdown 7.5 and its nodes render with `throwOnError`, so one bad formula breaks the view.
 *
 * The LaTeX source is the node's `value` attribute and comes back unchanged on save: a block the
 * student did not touch is written from its loaded text anyway (`preserve.ts`), and a changed one
 * serialises the source verbatim. A formula KaTeX cannot parse shows its source (`math-error`).
 * To change a formula, delete it and type `$...$` again (an input rule turns it into a formula).
 */

import { InputRule } from "@milkdown/prose/inputrules";
import { $inputRule, $nodeSchema, $remark } from "@milkdown/utils";
import remarkMath from "remark-math";
import { renderMathInto } from "../math/katex";

export const remarkMathPlugin = $remark("remarkMath", () => remarkMath);

export const mathInlineSchema = $nodeSchema("math_inline", () => ({
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  attrs: { value: { default: "" } },
  parseDOM: [{ tag: 'span[data-type="math_inline"]', getAttrs: (dom) => ({ value: (dom as HTMLElement).dataset.value ?? "" }) }],
  toDOM: (node) => {
    const element = document.createElement("span");
    element.dataset.type = "math_inline";
    element.dataset.value = String(node.attrs.value);
    element.className = "note-math";
    element.contentEditable = "false";
    renderMathInto(element, String(node.attrs.value), false);
    return element;
  },
  parseMarkdown: {
    match: (node) => node.type === "inlineMath",
    runner: (state, node, type) => {
      state.addNode(type, { value: String(node.value ?? "") });
    },
  },
  toMarkdown: {
    match: (node) => node.type.name === "math_inline",
    runner: (state, node) => {
      state.addNode("inlineMath", undefined, String(node.attrs.value));
    },
  },
}));

export const mathBlockSchema = $nodeSchema("math_block", () => ({
  group: "block",
  atom: true,
  isolating: true,
  defining: true,
  selectable: true,
  attrs: { value: { default: "" } },
  parseDOM: [{ tag: 'div[data-type="math_block"]', getAttrs: (dom) => ({ value: (dom as HTMLElement).dataset.value ?? "" }) }],
  toDOM: (node) => {
    const element = document.createElement("div");
    element.dataset.type = "math_block";
    element.dataset.value = String(node.attrs.value);
    element.className = "note-math note-math-block";
    element.contentEditable = "false";
    renderMathInto(element, String(node.attrs.value), true);
    return element;
  },
  parseMarkdown: {
    match: (node) => node.type === "math",
    runner: (state, node, type) => {
      state.addNode(type, { value: String(node.value ?? "") });
    },
  },
  toMarkdown: {
    match: (node) => node.type.name === "math_block",
    runner: (state, node) => {
      state.addNode("math", undefined, String(node.attrs.value));
    },
  },
}));

/** Typing `$x^2$` turns what was typed into a formula. */
export const mathInlineInputRule = $inputRule(
  (ctx) =>
    new InputRule(/(?<![$\\])\$(?![\s$])([^$\n]*[^$\s\\])\$$/, (state, match, start, end) => {
      const source = match[1];
      if (source === undefined) return null;
      const type = mathInlineSchema.type(ctx);
      return state.tr.replaceWith(start, end, type.create({ value: source }));
    }),
);

export const math = [remarkMathPlugin, mathInlineSchema, mathBlockSchema, mathInlineInputRule].flat();
