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
    // Column 2 is the draggable divider (#534).
    expect(document["grid-column"]).toBe("3");
    expect(document["grid-row"]).toBe("1");
    expect(detail["grid-column"]).toBe(document["grid-column"]);
    expect(detail["grid-row"]).toBe(document["grid-row"]);
    expect(Number(detail["z-index"])).toBeGreaterThan(0);
  });

  it("gives the chat column 40 % at every width so the document is at most 60 % (#534)", () => {
    const columns = declarations(workspaceCss, ".workspace-columns");
    expect(columns["--workspace-side"]).toBe("40%");
    expect(columns["grid-template-columns"]).toBe("var(--workspace-side) var(--space-4) minmax(0, 1fr)");
    expect(workspaceCss).not.toMatch(/--workspace-side:\s*clamp/);
  });

  it("shows the divider and the chat tools only in the two-column layout, and hides what they fold (#534)", () => {
    const always = outsideMedia(workspaceCss);
    expect(declarations(always, ".workspace-divider")["display"]).toBe("none");
    expect(declarations(always, ".workspace-toggle")["display"]).toBe("none");
    const twoColumns = mediaBlock(workspaceCss, TWO_COLUMNS);
    expect(declarations(twoColumns, ".workspace-divider")["display"]).toBe("block");
    expect(declarations(twoColumns, ".workspace-divider")["grid-column"]).toBe("2");
    expect(declarations(twoColumns, ".workspace-toggle")["position"]).toBe("absolute");
    expect(declarations(twoColumns, ".workspace-toggle")["right"]).toBe("var(--space-2)");
    expect(declarations(twoColumns, '.workspace[data-chat="expanded"] .workspace-sources')["display"]).toBe("none");
    expect(declarations(twoColumns, '.workspace .workspace-sources[data-collapsed="true"]')["flex"]).toBe("none");
    expect(
      declarations(twoColumns, '.workspace .workspace-sources[data-collapsed="true"] .workspace-tabpanel')["display"],
    ).toBe("none");
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

/** `css` without its `@media` blocks: the rules that apply at every width. */
function outsideMedia(css: string): string {
  let result = css.replace(/\/\*[\s\S]*?\*\//g, "");
  for (let start = result.indexOf("@media"); start >= 0; start = result.indexOf("@media")) {
    const open = result.indexOf("{", start);
    let depth = 0;
    let end = open;
    for (; end < result.length; end += 1) {
      if (result[end] === "{") depth += 1;
      if (result[end] === "}") depth -= 1;
      if (depth === 0) break;
    }
    result = result.slice(0, start) + result.slice(end + 1);
  }
  return result;
}

const ONE_COLUMN = "@media (max-width: 56.25rem) {";
const ONE_COLUMN_TALL = "@media (max-width: 56.25rem) and (min-height: 30rem)";

/**
 * #485: scrolling a long document scrolled its header (version, «Editar») away. jsdom does no
 * layout, so these tests pin the rules that keep it on screen: the document card is a column that
 * does not scroll, its header never shrinks and is sticky, and only the body under it scrolls; in
 * one column the document view is one viewport high the same way (and, on a short screen, the card
 * is no scroll container, so the header sticks to the top of the page).
 */
describe("the document's header stays pinned (#485)", () => {
  const base = outsideMedia(workspaceCss);

  it("scrolls only the body under the header in the document card", () => {
    const card = declarations(base, ".workspace-document");
    expect(card.display).toBe("flex");
    expect(card["flex-direction"]).toBe("column");
    expect(card.overflow).toBe("hidden");
    const header = declarations(base, ".workspace-document-header");
    expect(header.flex).toBe("none");
    expect(header.position).toBe("sticky");
    expect(header.top).toBe("0");
    expect(header.background).toBe("var(--surface)");
    const body = declarations(base, ".workspace-document-body");
    expect(body.flex).toBe("1 1 auto");
    expect(body["min-height"]).toBe("0");
    expect(body["overflow-y"]).toBe("auto");
    // The editor's column scrolls the same way.
    expect(declarations(base, ".workspace-document > .workspace-editing")["overflow-y"]).toBe("auto");
  });

  it("keeps the card a non-scrolling column in the two-column layout", () => {
    const card = declarations(mediaBlock(workspaceCss, TWO_COLUMNS), ".workspace-document");
    expect(card["min-height"]).toBe("0");
    expect(card.overflow).toBeUndefined();
    expect(card["overflow-y"]).toBeUndefined();
  });

  it("makes the one-column document view one viewport high, only the body scrolling", () => {
    const tall = mediaBlock(workspaceCss, ONE_COLUMN_TALL);
    const page = declarations(tall, '.workspace[data-view="document"]');
    expect(page.height).toMatch(/dvh/);
    expect(page.overflow).toBe("hidden");
    const card = declarations(tall, '.workspace[data-view="document"] .workspace-document');
    expect(card.display).toBe("flex");
    expect(card["min-height"]).toBe("0");
    expect(card.overflow).toBe("hidden");
    expect(declarations(tall, '.workspace[data-view="document"] .workspace-document-body')["overflow-y"]).toBe("auto");
    // The detail (#473) fills the document view's place the same way.
    expect(declarations(tall, '.workspace[data-view="document"] .workspace-detail')["min-height"]).toBe("0");
    // ...and is still what hides the document under it (a later rule, so it wins).
    expect(workspaceCss.lastIndexOf('.workspace[data-detail="open"] .workspace-document')).toBeGreaterThan(
      workspaceCss.indexOf(ONE_COLUMN_TALL),
    );
  });

  it("lets the header stick to the page on a short one-column screen", () => {
    const narrow = mediaBlock(workspaceCss, ONE_COLUMN);
    expect(declarations(narrow, ".workspace-document").overflow).toBe("visible");
    expect(declarations(narrow, ".workspace-document-body").overflow).toBe("visible");
  });
});

/**
 * #485: the workspace reads as zones -- a desk under a header band and three cards. jsdom does no
 * painting, so these tests pin that each zone takes its own token (the colours and their contrast
 * are checked in `styles/tokens.test.ts`).
 */
describe("the workspace's visual zones (#485)", () => {
  const base = outsideMedia(workspaceCss);

  it("puts the page on the desk under a solid header band", () => {
    expect(declarations(base, ".workspace").background).toBe("var(--desk)");
    const band = declarations(base, ".workspace-bar");
    expect(band.background).toBe("var(--header-bg)");
    expect(band.color).toBe("var(--on-header)");
    expect(declarations(base, ".workspace-title").color).toBe("var(--on-header)");
    expect(declarations(base, ".workspace-bar-end a").color).toBe("var(--on-header-muted)");
    expect(declarations(base, ".workspace-bar :focus-visible")["outline-color"]).toBe("var(--on-header)");
    expect(declarations(base, ".workspace-bar .mode-switch")["border-color"]).toBe("var(--header-line)");
  });

  it("makes Captura/Recursos, the chat and the document three cards with their own surfaces", () => {
    for (const selector of [".workspace-sources", ".workspace-chat", ".workspace-document"]) {
      const card = declarations(base, selector);
      expect(card.border, selector).toBe("1px solid var(--line)");
      expect(card["border-radius"], selector).toBe("var(--radius-l)");
      expect(card["box-shadow"], selector).toBe("var(--card-shadow)");
    }
    expect(declarations(base, ".workspace-sources").background).toBe("var(--paper)");
    expect(declarations(base, ".workspace-chat").background).toBe("var(--chat-surface)");
    expect(declarations(base, ".workspace-document").background).toBe("var(--surface)");
  });

  it("uses no literal colour in the zones' rules", () => {
    for (const selector of [".workspace", ".workspace-bar", ".workspace-sources", ".workspace-chat", ".workspace-document", ".workspace-document-header"]) {
      for (const [property, value] of Object.entries(declarations(base, selector))) {
        expect(value, `${selector} ${property}`).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i);
      }
    }
  });
});
