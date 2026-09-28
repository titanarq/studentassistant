import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import NotesView from "./NotesView";
import { parseNotes } from "./markdown";
import { notesImageUrl } from "./api";

const NOTES =
  "# Tema\n\n## Triángulo {#triangulo}\n\n![Diagrama 1](../sources/images/img-001.svg)[^img001]\n\n" +
  "[^img001]: [Diagrama 1](../sources/images/img-001.svg)\n";

it("draws an SVG diagram the editor drew as an <img> through the read API, never inline (#511)", () => {
  const { container } = render(
    <NotesView
      tree={parseNotes(NOTES)}
      onOpenSource={() => undefined}
      resolveImage={(src) => notesImageUrl("mates", "geometria", src)}
    />,
  );
  const image = screen.getByRole("img", { name: "Diagrama 1" });
  expect(image.tagName).toBe("IMG");
  expect(image).toHaveAttribute("src", "/api/sources/subjects/mates/topics/geometria/sources/images/img-001.svg");
  // The drawing is never inserted as markup: no <svg> element comes from the notes.
  expect(container.querySelector("svg polygon, svg script")).toBeNull();
});
