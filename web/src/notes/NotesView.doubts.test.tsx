import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import NotesView from "./NotesView";
import { parseNotes } from "./markdown";
import { badgesOf, type DoubtMark, type DoubtMarks, nextDoubt, pickDoubt, readDoubtMarks } from "../workspace/doubtMarks";

const NOTES = `# Revolución industrial

## 1. Causas {#causas}

El carbón y el hierro.[^p3]

La máquina de vapor en 1769.[^p3][^b83]

## 2. Consecuencias {#consecuencias}

Crecen las ciudades.[^p4]

[^p3]: [Apuntes, página 3](../sources/notes/page-003.jpg)
[^p4]: [Apuntes, página 4](../sources/notes/page-004.jpg)
[^b83]: [Libro, página 83](../sources/book/page-083.jpg)
`;

const mark = (pendingId: string, fields: Partial<DoubtMark>): DoubtMark => ({
  pendingId,
  kind: "illegible",
  text: "",
  level: "block",
  blocks: [],
  section: null,
  asked: false,
  ...fields,
});

const MARKS: DoubtMarks = {
  count: 4,
  marks: [
    mark("d-top", { level: "top" }),
    mark("d-1", { blocks: [{ section: "causas", number: 2 }] }),
    mark("d-2", { blocks: [{ section: "causas", number: 2 }], asked: true }),
    mark("d-3", { level: "section", section: "consecuencias" }),
  ],
};

it("marks the doubts in the margin, on a section heading and at the top, never in the text (#516)", () => {
  const onOpenDoubts = vi.fn();
  const { container } = render(
    <NotesView tree={parseNotes(NOTES)} onOpenSource={() => undefined} doubts={badgesOf(MARKS)} onOpenDoubts={onOpenDoubts} />,
  );
  const vapor = screen.getByText(/La máquina de vapor/).closest(".notes-block") as HTMLElement;
  const badge = within(vapor).getByRole("button", { name: "2 dudas abiertas en este párrafo: ver una en el chat" });
  expect(badge).toHaveTextContent("?2");
  expect(vapor).toHaveClass("notes-doubted");
  // The block without doubts has no badge.
  const carbon = screen.getByText(/El carbón/).closest(".notes-block") as HTMLElement;
  expect(within(carbon).queryByRole("button", { name: /duda/ })).toBeNull();

  const heading = screen.getByRole("heading", { name: /Consecuencias/ }).closest(".notes-block") as HTMLElement;
  expect(within(heading).getByRole("button", { name: "1 duda abierta sobre esta sección: verla en el chat" })).toHaveTextContent("?");

  expect(screen.getByRole("note")).toHaveTextContent("Hay 1 duda sobre todo el tema");
  fireEvent.click(screen.getByRole("button", { name: "1 duda abierta sobre todo el tema: verla en el chat" }));
  expect(onOpenDoubts).toHaveBeenLastCalledWith(["d-top"], expect.any(HTMLElement));
  fireEvent.click(badge);
  expect(onOpenDoubts).toHaveBeenLastCalledWith(["d-1", "d-2"], badge);
  // The notes' text is untouched: no doubt text or mark in the paragraphs.
  expect(container.querySelector("article")?.textContent).not.toMatch(/\[\[\?/);
});

it("draws nothing without marks, and disables the badges while a doubt is on its way", () => {
  const { rerender } = render(<NotesView tree={parseNotes(NOTES)} onOpenSource={() => undefined} doubts={badgesOf(null)} />);
  expect(screen.queryByRole("button", { name: /duda/ })).toBeNull();
  expect(screen.queryByRole("note")).toBeNull();
  rerender(<NotesView tree={parseNotes(NOTES)} onOpenSource={() => undefined} doubts={badgesOf(MARKS)} onOpenDoubts={vi.fn()} doubtsDisabled />);
  for (const button of screen.getAllByRole("button", { name: /duda/ })) expect(button).toBeDisabled();
});

it("reads the marks leniently and picks the first doubt not asked yet", () => {
  const read = readDoubtMarks({
    subject: "historia",
    topic: "revolucion-industrial",
    count: 3,
    marks: [
      { pending_id: "a", kind: "illegible", text: "x", level: "block", blocks: [{ section: "causas", number: 1 }, { number: 0 }], asked: true },
      { pending_id: "b", kind: "incomplete", text: "y", level: "section", section: "causas" },
      { pending_id: "c", kind: "incomplete", text: "z", level: "top", blocks: [] },
      { nope: true },
    ],
  });
  expect(read?.count).toBe(3);
  expect(read?.marks.map((m) => [m.pendingId, m.level])).toEqual([
    ["a", "block"],
    ["b", "section"],
    ["c", "top"],
  ]);
  expect(read?.marks[0].blocks).toEqual([{ section: "causas", number: 1 }]);
  expect(nextDoubt(read)).toBe("b");
  expect(pickDoubt(read, ["a"])).toBe("a");
  expect(pickDoubt(read, [])).toBeNull();
  expect(readDoubtMarks({ marks: "no" })).toBeNull();
});
