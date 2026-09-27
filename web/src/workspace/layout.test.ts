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
