import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Read from disk: vitest turns CSS imports (even `?raw`) into empty modules.
const here = dirname(fileURLToPath(import.meta.url));
const workspaceCss = readFileSync(join(here, "workspace.css"), "utf8");
const chatCss = readFileSync(join(here, "chat", "chat.css"), "utf8");

/**
 * #463: after a long reply (with its diff open) the chat's input and «Enviar» went below the screen,
 * clipped by the left column, because `.workspace-chat` had `min-height: min-content` and so grew
 * to the whole log's height. jsdom does no layout, so these tests pin the rules that keep the form
 * on screen in the two-column workspace: the chat takes what is left of the column and may shrink
 * to nothing, the form never shrinks, and only the log shrinks and scrolls.
 */

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
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const result: Record<string, string> = {};
  const rule = /([^{}]+)\{([^{}]*)\}/g;
  for (const match of withoutComments.matchAll(rule)) {
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

describe("workspace chat layout (#463)", () => {
  const twoColumns = mediaBlock(workspaceCss, TWO_COLUMNS);

  it("never lets the chat grow past its share of the left column", () => {
    const chat = declarations(twoColumns, ".workspace-chat");
    expect(chat["min-height"]).toBe("0");
    expect(chat.flex).toBe("1 1 0");
    expect(chat.overflow).toBe("hidden");
    expect(twoColumns.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/min-content/);
    expect(declarations(twoColumns, ".workspace-left")["min-height"]).toBe("0");
    expect(declarations(twoColumns, ".workspace-chat > .ws-chat")["min-height"]).toBe("0");
  });

  it("keeps the form at its height and lets only the log shrink and scroll", () => {
    expect(declarations(chatCss, ".ws-chat-form").flex).toBe("none");
    expect(declarations(chatCss, ".ws-chat-scroll")["min-height"]).toBe("0");
    const log = declarations(chatCss, ".ws-chat-log");
    expect(log["min-height"]).toBe("0");
    expect(log["overflow-y"]).toBe("auto");
  });

  it("caps the input's height so dragging it taller cannot push «Enviar» out", () => {
    expect(declarations(twoColumns, ".workspace-chat .ws-chat-form textarea")["max-height"]).toMatch(/dvh/);
  });
});

/**
 * #473: the checkbox, trash button and corner badges over a thumbnail used near-opaque paper
 * backgrounds that hid the page. jsdom does no painting, so these tests pin that each control is
 * translucent (the glass veil), keeps a ring and a blurred backdrop so it stays legible on light
 * and dark images, and still has distinct hover, focus and selected states.
 */
describe("thumbnail controls over the image (#473)", () => {
  /** The alpha of an `rgb(r g b / NN%)` colour, as a fraction. */
  function alpha(color: string): number {
    const match = /\/\s*([\d.]+)%\s*\)/.exec(color);
    expect(match, `no alpha in ${color}`).not.toBeNull();
    return Number(match![1]) / 100;
  }

  const glass = declarations(workspaceCss, ".resource-card");

  it("gives every control a translucent veil, a ring and a blurred backdrop", () => {
    expect(alpha(glass["--glass-veil"])).toBeLessThanOrEqual(0.35);
    expect(alpha(glass["--glass-ok"])).toBeLessThanOrEqual(0.65);
    expect(alpha(glass["--glass-warn"])).toBeLessThanOrEqual(0.65);
    for (const selector of [".resource-check", ".resource-delete", ".resource-badge"]) {
      const rule = declarations(workspaceCss, selector);
      expect(rule["backdrop-filter"], selector).toMatch(/blur/);
      expect(rule.border, selector).toContain("var(--glass-ring)");
    }
    expect(declarations(workspaceCss, ".resource-check").background).toBe("var(--glass-veil)");
    expect(declarations(workspaceCss, ".resource-delete").background).toBe("var(--glass-veil)");
    expect(declarations(workspaceCss, ".resource-badge-in").background).toBe("var(--glass-ok)");
    expect(declarations(workspaceCss, ".resource-badge-warn").background).toBe("var(--glass-warn)");
    expect(workspaceCss).not.toMatch(/var\(--paper\) 88%/);
  });

  it("keeps hover, focus and selected clearly different from the resting state", () => {
    expect(declarations(workspaceCss, ".resource-check:hover").background).toBe("var(--glass-veil-strong)");
    expect(declarations(workspaceCss, ".resource-check:has(input:focus-visible)").outline).toMatch(/accent/);
    expect(declarations(workspaceCss, ".resource-selected .resource-check").background).toBe("var(--accent)");
    expect(declarations(workspaceCss, ".resource-delete:hover").background).toBe("var(--glass-danger)");
    expect(declarations(workspaceCss, ".resource-delete:focus-visible").outline).toMatch(/accent/);
  });
});

/**
 * #473 (review of #477): the detail was pinned to column 2 / row 1 while the left column and the
 * document were auto-placed, so opening it pushed the document into a new row under column 1 and
 * collapsed the left column and the detail to zero height. jsdom does no grid layout, so these tests
 * pin every item of the columns' grid to its cell: the left column alone in column 1, the document
 * and the detail sharing column 2, row 1, with the detail stacked on top.
 */
describe("the source's detail over the document column (#473)", () => {
  it("places the left column, the document and the detail in explicit cells", () => {
    const left = declarations(workspaceCss, ".workspace-left");
    expect(left["grid-column"]).toBe("1");
    expect(left["grid-row"]).toBe("1");
    const document = declarations(workspaceCss, ".workspace-document");
    const detail = declarations(workspaceCss, ".workspace-detail");
    expect(document["grid-column"]).toBe("2");
    expect(document["grid-row"]).toBe("1");
    expect(detail["grid-column"]).toBe(document["grid-column"]);
    expect(detail["grid-row"]).toBe(document["grid-row"]);
    expect(Number(detail["z-index"])).toBeGreaterThan(0);
  });

  it("keeps a single row in the two-column layout", () => {
    const twoColumns = mediaBlock(workspaceCss, TWO_COLUMNS);
    expect(declarations(twoColumns, ".workspace-columns")["grid-template-rows"]).toBe("minmax(0, 1fr)");
    for (const selector of [".workspace-left", ".workspace-document", ".workspace-detail"]) {
      const rule = declarations(twoColumns, selector);
      expect(rule["grid-column"], selector).toBeUndefined();
      expect(rule["grid-row"], selector).toBeUndefined();
    }
  });
});
