import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { fakeMermaid } from "../test/fakeMermaid";
import { parseNotes } from "./markdown";
import { resetMermaidForTests } from "./mermaid";
import NotesView from "./NotesView";

const DIAGRAM = "graph TD\n  A[Causas] --> B[Revolución]";

function renderNotes(text: string) {
  return render(<NotesView tree={parseNotes(text)} onOpenSource={() => undefined} />);
}

/** A controllable `prefers-color-scheme: dark` media query. */
function stubColorScheme(dark: boolean) {
  const listeners = new Set<() => void>();
  const query = {
    matches: dark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
  };
  window.matchMedia = (() => query) as unknown as typeof window.matchMedia;
  return (next: boolean) => {
    query.matches = next;
    for (const listener of listeners) listener();
  };
}

const originalMatchMedia = window.matchMedia;

beforeEach(() => {
  fakeMermaid.initialize.mockClear();
  fakeMermaid.render.mockClear();
});

afterEach(() => {
  window.matchMedia = originalMatchMedia;
  resetMermaidForTests();
});

// First in the file: the module registry still has not imported mermaid.
it("does not load mermaid for notes without a diagram", async () => {
  renderNotes("# Apuntes\n\n```python\nprint(1)\n```\n");
  await act(async () => undefined);
  expect(fakeMermaid.loads).toBe(0);
  expect(screen.getByText("print(1)")).toBeInTheDocument();
});

it("loads mermaid lazily and draws a mermaid fence as an SVG with strict security", async () => {
  renderNotes(`# Apuntes\n\n\`\`\`mermaid\n${DIAGRAM}\n\`\`\`\n\nDespués del diagrama.`);
  // Before the lazy import resolves, the source shows as code.
  expect(screen.getByText(/A\[Causas\]/)).toBeInTheDocument();
  const svg = await screen.findByTestId("mermaid-svg");
  expect(fakeMermaid.loads).toBe(1);
  expect(svg.closest("figure")).toHaveClass("notes-mermaid");
  expect(fakeMermaid.render).toHaveBeenCalledWith(expect.any(String), DIAGRAM);
  expect(fakeMermaid.initialize).toHaveBeenCalledWith(
    expect.objectContaining({ startOnLoad: false, securityLevel: "strict", theme: "default" }),
  );
  expect(screen.queryByText(/A\[Causas\]/)).not.toBeInTheDocument();
  expect(screen.getByText("Después del diagrama.")).toBeInTheDocument();
});

it("shows the source and a Spanish message when the diagram does not parse", async () => {
  renderNotes(`\`\`\`mermaid\ninvalid ->\n\`\`\`\n\n\`\`\`mermaid\n${DIAGRAM}\n\`\`\`\n\nEl resto sigue.`);
  expect(await screen.findByText("No se pudo dibujar el diagrama")).toBeInTheDocument();
  expect(screen.getByText("invalid ->").closest("pre")).toHaveClass("notes-code");
  // The other diagram and the rest of the notes are unaffected.
  expect(await screen.findByTestId("mermaid-svg")).toBeInTheDocument();
  expect(screen.getByText("El resto sigue.")).toBeInTheDocument();
});

it("uses the dark theme under prefers-color-scheme: dark and re-renders when it changes", async () => {
  const setDark = stubColorScheme(true);
  renderNotes(`\`\`\`mermaid\n${DIAGRAM}\n\`\`\``);
  expect(await screen.findByTestId("mermaid-svg")).toHaveAttribute("data-theme", "dark");
  expect(fakeMermaid.initialize).toHaveBeenLastCalledWith(expect.objectContaining({ theme: "dark" }));

  act(() => setDark(false));
  await expect.poll(() => screen.getByTestId("mermaid-svg").getAttribute("data-theme")).toBe("default");
  expect(fakeMermaid.initialize).toHaveBeenLastCalledWith(expect.objectContaining({ theme: "default" }));
  expect(fakeMermaid.render).toHaveBeenCalledTimes(2);
});
