/**
 * The lazily loaded mermaid renderer of the notes (#479). `mermaid` is a large package, so it is
 * only fetched with a dynamic `import()` the first time a note actually carries a ```mermaid
 * fence: pages without diagrams never load it and it stays out of the main bundle.
 *
 * Renders are chained one after another because `mermaid.initialize` is global: a theme switch
 * between two renders must not leak into a render that is already running.
 */

export type MermaidTheme = "default" | "dark";

type Mermaid = (typeof import("mermaid"))["default"];

let loading: Promise<Mermaid> | null = null;
let queue: Promise<unknown> = Promise.resolve();
let initializedTheme: MermaidTheme | null = null;
let counter = 0;

/** The mermaid module, imported once on first use. */
export function loadMermaid(): Promise<Mermaid> {
  if (loading === null) {
    loading = import("mermaid").then((module) => module.default);
    loading.catch(() => {
      loading = null; // a failed chunk load is retried on the next diagram
    });
  }
  return loading;
}

/** The SVG markup of one diagram; rejects when mermaid cannot parse `source`. */
export function renderMermaid(source: string, theme: MermaidTheme): Promise<string> {
  const run = queue.then(async () => {
    const mermaid = await loadMermaid();
    if (initializedTheme !== theme) {
      // `strict`: mermaid sanitizes labels and disables click handlers and HTML in the diagram.
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme, suppressErrorRendering: true });
      initializedTheme = theme;
    }
    const id = `notes-mermaid-${++counter}`;
    try {
      const { svg } = await mermaid.render(id, source);
      return svg;
    } finally {
      // mermaid renders in a temporary element of the body; never leave one behind on failure.
      document.getElementById(id)?.remove();
      document.getElementById(`d${id}`)?.remove();
    }
  });
  queue = run.catch(() => undefined);
  return run;
}

/** Forgets the loaded module and theme; only tests call it. */
export function resetMermaidForTests() {
  loading = null;
  queue = Promise.resolve();
  initializedTheme = null;
  counter = 0;
}
