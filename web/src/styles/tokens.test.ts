import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Read from disk: vitest turns CSS imports (even `?raw`) into empty modules.
const here = dirname(fileURLToPath(import.meta.url));
const tokensCss = readFileSync(join(here, "tokens.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** The custom properties declared in the first `:root { ... }` block of `css`. */
function rootTokens(css: string): Record<string, string> {
  const match = /:root\s*\{([^{}]*)\}/.exec(css);
  expect(match, "no :root block").not.toBeNull();
  const tokens: Record<string, string> = {};
  for (const declaration of match![1].split(";")) {
    const colon = declaration.indexOf(":");
    const name = declaration.slice(0, colon).trim();
    if (colon < 0 || !name.startsWith("--")) continue;
    tokens[name] = declaration.slice(colon + 1).trim();
  }
  return tokens;
}

const DARK_QUERY = "@media (prefers-color-scheme: dark)";

/** The light theme (`:root`) and the dark one (its own `:root` inside the media query, over light). */
function themes(): Record<"light" | "dark", Record<string, string>> {
  const light = rootTokens(tokensCss);
  const start = tokensCss.indexOf(DARK_QUERY);
  expect(start, "no dark theme").toBeGreaterThanOrEqual(0);
  return { light, dark: { ...light, ...rootTokens(tokensCss.slice(start + DARK_QUERY.length)) } };
}

/** A token's value as `#rrggbb`, following `var(--other)` references. */
function hex(tokens: Record<string, string>, name: string): string {
  let value = tokens[name];
  for (let hops = 0; value !== undefined && hops < 5; hops += 1) {
    const reference = /^var\((--[\w-]+)\)$/.exec(value);
    if (reference === null) break;
    value = tokens[reference[1]];
  }
  expect(value, `${name} is not an opaque #rrggbb colour`).toMatch(/^#[0-9a-f]{6}$/i);
  return value;
}

/** WCAG 2.x relative luminance of `#rrggbb`. */
function luminance(color: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const channel = parseInt(color.slice(i, i + 2), 16) / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG 2.x contrast ratio of two `#rrggbb` colours. */
function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

/**
 * #485: the workspace's zones -- the desk, the header band, the chat card -- bring new
 * backgrounds. Every text colour used on them must reach WCAG AA (4.5:1) and every border or
 * focus ring that marks a control on them 3:1, in both themes.
 */
const TEXT: Array<[string, string]> = [
  // The header band: the topic's name, its links, the switch's failure line.
  ["--on-header", "--header-bg"],
  ["--on-header-muted", "--header-bg"],
  // The chat card: the log, the status lines, links, warnings and failures.
  ["--ink", "--chat-surface"],
  ["--ink-muted", "--chat-surface"],
  ["--accent", "--chat-surface"],
  ["--correction", "--chat-surface"],
  ["--warn", "--chat-surface"],
  ["--ok", "--chat-surface"],
  // The desk between the cards (the single-column switch, anything that falls on it).
  ["--ink", "--desk"],
  ["--ink-muted", "--desk"],
  ["--accent", "--desk"],
  // The document sheet's pinned header, and the sources card.
  ["--ink", "--surface"],
  ["--ink-muted", "--surface"],
  ["--ink", "--paper"],
  ["--ink-muted", "--paper"],
];

const UI: Array<[string, string]> = [
  // The mode switch's edge and the focus ring in the header band.
  ["--header-line", "--header-bg"],
  ["--on-header", "--header-bg"],
  // The focus ring on the chat card and on the desk.
  ["--accent", "--chat-surface"],
  ["--accent", "--desk"],
];

describe("the workspace zones' tokens (#485)", () => {
  const all = themes();

  for (const theme of ["light", "dark"] as const) {
    const tokens = all[theme];

    it(`defines every zone token in the ${theme} theme`, () => {
      for (const name of ["--desk", "--header-bg", "--on-header", "--on-header-muted", "--header-line", "--chat-surface"]) {
        hex(tokens, name);
      }
      expect(tokens["--card-shadow"]).toMatch(/rgb\(/);
    });

    it(`gives text on the zones at least 4.5:1 in the ${theme} theme`, () => {
      for (const [ink, ground] of TEXT) {
        const ratio = contrast(hex(tokens, ink), hex(tokens, ground));
        expect(ratio, `${ink} on ${ground}: ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
      }
    });

    it(`gives borders and focus rings on the zones at least 3:1 in the ${theme} theme`, () => {
      for (const [line, ground] of UI) {
        const ratio = contrast(hex(tokens, line), hex(tokens, ground));
        expect(ratio, `${line} on ${ground}: ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(3);
      }
    });

    it(`keeps the zones apart in the ${theme} theme`, () => {
      // The desk is darker than both card surfaces, so the cards stand on it.
      const desk = luminance(hex(tokens, "--desk"));
      expect(desk).toBeLessThan(luminance(hex(tokens, "--paper")));
      expect(desk).toBeLessThan(luminance(hex(tokens, "--surface")));
      // The chat's tint is its own, and the header band is none of the surfaces.
      const surfaces = ["--paper", "--surface", "--desk"].map((name) => hex(tokens, name).toLowerCase());
      expect(surfaces).not.toContain(hex(tokens, "--chat-surface").toLowerCase());
      expect([...surfaces, hex(tokens, "--chat-surface").toLowerCase()]).not.toContain(
        hex(tokens, "--header-bg").toLowerCase(),
      );
    });
  }

  it("changes every zone colour for the dark theme", () => {
    for (const name of ["--desk", "--card-shadow", "--header-bg", "--on-header", "--on-header-muted", "--header-line", "--chat-surface"]) {
      expect(all.dark[name], name).not.toBe(all.light[name]);
    }
  });
});
