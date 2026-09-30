import { Fragment, type ReactNode } from "react";
import { mathSource, renderMath } from "./katex";

/** A formula, rendered with KaTeX; the raw `$source$` in a `math-error` span when it does not parse. */
export default function MathView({ source, display = false }: { source: string; display?: boolean }) {
  const html = renderMath(source, display);
  // Always a span: a display formula may sit inside a paragraph (`math-display` makes it a block).
  const Tag = "span";
  if (html === null) {
    return <Tag className={display ? "math-error math-display" : "math-error"}>{mathSource(source, display)}</Tag>;
  }
  return <Tag className={display ? "math math-display" : "math"} dangerouslySetInnerHTML={{ __html: html }} />;
}

const MATH = /\$\$([^$]+?)\$\$|\$(?![\s$])((?:\\.|[^$\\])+?)(?<!\s)\$(?!\d)/g;

/**
 * Plain text with its formulas rendered: a chat bubble that shows the model's text as it is
 * (paragraphs, no Markdown) still shows `$f'(x)$` and `$$...$$` as mathematics.
 */
export function MathText({ text }: { text: string }): ReactNode {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(MATH)) {
    const source = (match[1] ?? match[2] ?? "").trim();
    if (source === "") continue;
    const index = match.index ?? 0;
    if (index > last) parts.push(text.slice(last, index));
    parts.push(<MathView key={index} source={source} display={match[1] !== undefined} />);
    last = index + match[0].length;
  }
  if (parts.length === 0) return text;
  if (last < text.length) parts.push(text.slice(last));
  return <Fragment>{parts}</Fragment>;
}
