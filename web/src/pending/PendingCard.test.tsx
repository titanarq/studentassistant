import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import PendingCard, { describeRefs } from "./PendingCard";
import { item } from "./testPending";

it.each([
  ["contradiction", "Contradicción", "Tus fuentes no dicen lo mismo: habrá que elegir cuál vale."],
  ["possible_error", "Posible error", "Puede que aquí haya un error."],
  ["illegible", "Ilegible", "Esta parte no se pudo leer bien."],
  ["incomplete", "Incompleto", "A esta parte le falta algo."],
  ["unexplained_concept", "Concepto sin explicar", "Aparece un concepto que no se explica."],
])("renders a %s card with its title and hint", (kind, title, hint) => {
  render(<PendingCard item={item({ kind, text: "Una duda." })} />);

  const card = screen.getByRole("article", { name: `${title}: Una duda.` });
  expect(card).toHaveClass(`pending-card-${kind}`);
  expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
  expect(card).toHaveTextContent(hint);
  expect(card).toHaveTextContent("Abierta");
});

it("says what the doubt refers to and how many doubts were merged", () => {
  render(
    <PendingCard
      item={item({ refs: { pages: ["c1", "c2"], segments: ["s1"], sources: ["b1"] }, merged_ids: ["p7", "p8"] })}
    />,
  );

  expect(screen.getByText("Sobre: 2 páginas · 1 fragmento de la conversación · 1 fuente")).toBeInTheDocument();
  expect(screen.getByText("Se juntó con otras 2 dudas iguales.")).toBeInTheDocument();
});

it("shows how a closed doubt was closed, without the open hint", () => {
  render(<PendingCard item={item({ status: "auto_resolved", resolution: "El libro dice 14 de julio de 1789." })} />);

  expect(screen.getByText("Resuelta con las fuentes")).toBeInTheDocument();
  expect(screen.getByText("Resolución: El libro dice 14 de julio de 1789.")).toBeInTheDocument();
  expect(screen.queryByText("Esta parte no se pudo leer bien.")).not.toBeInTheDocument();
});

it("describes no refs as nothing", () => {
  expect(describeRefs({ pages: [], segments: [], sources: [] })).toBeNull();
});
