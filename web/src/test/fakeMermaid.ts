import { vi } from "vitest";

/**
 * The stand-in for the `mermaid` package in every test (#479): jsdom cannot lay out SVG, so no
 * test draws a real diagram. `render` answers an SVG naming the theme it was initialized with,
 * and rejects a source whose first line is `invalid`, as mermaid does for a parse error.
 */
let theme = "default";

export const fakeMermaid = {
  /** How many times the module was imported; the dynamic `import()` of mermaid.ts counts here. */
  loads: 0,
  initialize: vi.fn((config: { theme?: string }) => {
    theme = config.theme ?? "default";
  }),
  render: vi.fn(async (id: string, source: string) => {
    if (source.trimStart().startsWith("invalid")) throw new Error("Parse error on line 1");
    return { svg: `<svg id="${id}" data-testid="mermaid-svg" data-theme="${theme}"><text>${source.length}</text></svg>`, diagramType: "flowchart" };
  }),
};
