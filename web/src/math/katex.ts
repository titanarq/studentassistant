/**
 * KaTeX for the web UI (#532): the one place that turns a LaTeX source into markup. The fonts and
 * the stylesheet come from the `katex` package (bundled by Vite, no CDN). A formula KaTeX cannot
 * parse never throws to the caller: `renderMath` returns `null` and the view shows the source.
 */

import katex from "katex";
import "katex/dist/katex.min.css";
import "./math.css";

/** The markup of `source`, or `null` when KaTeX rejects it. KaTeX escapes the source it renders. */
export function renderMath(source: string, display: boolean): string | null {
  try {
    return katex.renderToString(source, {
      displayMode: display,
      throwOnError: true,
      strict: "ignore",
      trust: false,
      output: "htmlAndMathml",
    });
  } catch {
    return null;
  }
}

/** The formula as written, the way a malformed one is shown. */
export function mathSource(source: string, display: boolean): string {
  return display ? `$$${source}$$` : `$${source}$`;
}

/** The same into a DOM element (the visual editor's); the raw source when it does not parse. */
export function renderMathInto(element: HTMLElement, source: string, display: boolean): void {
  const html = renderMath(source, display);
  if (html === null) {
    element.classList.add("math-error");
    element.textContent = mathSource(source, display);
  } else {
    element.classList.remove("math-error");
    element.innerHTML = html;
  }
}
