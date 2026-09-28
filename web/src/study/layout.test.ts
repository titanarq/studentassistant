import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Read from disk: vitest turns CSS imports (even `?raw`) into empty modules.
const here = dirname(fileURLToPath(import.meta.url));
const studyCss = readFileSync(join(here, "study.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const studyChatCss = readFileSync(join(here, "chat", "studyChat.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const TWO_COLUMNS = "@media (min-width: 56.3125rem) and (min-height: 30rem)";

/** The body of the `@media` block that starts with `query` (braces balanced). */
function mediaBlock(css: string, query: string): string {
  const start = css.indexOf(query);
  expect(start, `no ${query} block`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === "{") depth += 1;
    if (css[i] === "}") depth -= 1;
    if (depth === 0) return css.slice(open + 1, i);
  }
  throw new Error(`unbalanced ${query} block`);
}

/** Every declaration of the rules whose selector is exactly `selector`, the last one winning. */
function declarations(css: string, selector: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = match[1].split(",").map((s) => s.trim());
    if (!selectors.includes(selector)) continue;
    for (const declaration of match[2].split(";")) {
      const colon = declaration.indexOf(":");
      if (colon < 0) continue;
      result[declaration.slice(0, colon).trim()] = declaration.slice(colon + 1).trim();
    }
  }
  return result;
}

/** Every selector of every rule in `css`. */
function selectors(css: string): string[] {
  return [...css.matchAll(/([^{}@]+)\{[^{}]*\}/g)].flatMap((match) => match[1].split(",").map((s) => s.trim()));
}

/**
 * #487: Estudiar shares Construir's frame (`WorkspaceFrame` and `workspace.css`). jsdom does no
 * layout, so these tests pin that the study styles never redefine the frame -- the page, the
 * header band, the columns, the cards, the one-column switch -- and that inside it the options'
 * body, the document's body and the material's body are the parts that scroll.
 */
describe("study screen in the workspace's frame (#487)", () => {
  it("leaves the page, the header band, the columns, the cards and the switch to the frame", () => {
    const frame = /^\.workspace(-header|-bar|-columns|-left|-sources|-chat|-document|-switch|-detail)?$/;
    const own = [...selectors(studyCss), ...selectors(studyChatCss)];
    expect(own.filter((selector) => frame.test(selector))).toEqual([]);
    // No page of its own any more: no width, gutter or one-column switch of the old study screen.
    expect(own.some((selector) => /\.study-(columns|left|switch|header|title)\b/.test(selector))).toBe(false);
    expect(declarations(studyCss, ".study")["max-width"]).toBeUndefined();
    expect(declarations(studyCss, ".study").padding).toBeUndefined();
  });

  it("scrolls only the options' body inside their card in two columns", () => {
    const body = declarations(mediaBlock(studyCss, TWO_COLUMNS), ".study-card-body");
    expect(body["min-height"]).toBe("0");
    expect(body["overflow-y"]).toBe("auto");
    expect(body.flex).toBe("1 1 auto");
  });

  it("puts the material's panel under the document's header, its own header pinned", () => {
    const area = declarations(studyCss, ".study-document-area");
    expect(area.position).toBe("relative");
    expect(area["min-height"]).toBe("0");
    expect(declarations(studyCss, ".study-panel").position).toBe("absolute");
    const header = declarations(studyCss, ".study-panel-header");
    expect(header.position).toBe("sticky");
    expect(header.flex).toBe("none");
    const body = declarations(studyCss, ".study-panel-body");
    expect(body["overflow-y"]).toBe("auto");
    expect(body["min-height"]).toBe("0");
  });

  it("lays the options out as cards two per row, each a whole button with a clear focus (#509)", () => {
    const list = declarations(studyCss, ".study-option-list");
    expect(list.display).toBe("grid");
    expect(list["grid-template-columns"]).toBe("repeat(2, minmax(0, 1fr))");
    const card = declarations(studyCss, ".study-option");
    expect(card.width).toBe("100%");
    expect(card.border).toMatch(/var\(--line\)/);
    expect(declarations(studyCss, ".study-option:focus-visible").outline).toBe("var(--focus-ring)");
    expect(declarations(studyCss, '.study-option[aria-expanded="true"]').background).toBe("var(--accent-soft)");
    expect(studyCss).not.toMatch(/\.study-(reviews|review-now|option-description)\b/);
  });

  it("uses no literal colour in its own rules", () => {
    expect(studyCss).not.toMatch(/#[0-9a-f]{3,8}\b|rgb\(/i);
  });
});
