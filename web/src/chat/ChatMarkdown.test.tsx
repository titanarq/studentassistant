import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import ChatMarkdown from "./ChatMarkdown";

it("draws bullets and bold instead of the literal marks", () => {
  render(<ChatMarkdown text={"- **Colocación como en la ficha:** Código arriba.\n- Segunda idea"} />);

  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByText("Colocación como en la ficha:").tagName).toBe("STRONG");
  expect(document.body.textContent).not.toContain("**");
});

it("draws numbered lists, italic, code and blockquotes", () => {
  const { container } = render(<ChatMarkdown text={"1. uno\n2. dos\n\nUn *énfasis* y `x = 1`.\n\n> cita"} />);

  expect(container.querySelector("ol")?.children).toHaveLength(2);
  expect(container.querySelector("em")).toHaveTextContent("énfasis");
  expect(container.querySelector("code")).toHaveTextContent("x = 1");
  expect(container.querySelector("blockquote")).toHaveTextContent("cita");
});

it("never injects HTML from the reply", () => {
  const { container } = render(
    <ChatMarkdown text={'<img src=x onerror="alert(1)"> <script>alert(1)</script> [malo](javascript:alert(1))'} />,
  );

  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("a")).toBeNull();
  expect(container.textContent).toContain("<script>");
});

it("opens safe links without giving the page away", () => {
  render(<ChatMarkdown text="[apuntes](https://example.com/a)" />);

  const link = screen.getByRole("link", { name: "apuntes" });
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  expect(link).toHaveAttribute("target", "_blank");
});

it("keeps footnote marks visible and survives half-written Markdown while streaming", () => {
  const { rerender } = render(<ChatMarkdown text="Dato clave [^p4]" />);
  expect(screen.getByText("[p4]")).toBeInTheDocument();

  for (const partial of ["**neg", "- **a** y *b", "`co", "> ", "1.", "[enlace](http", "", "\n\n"]) {
    rerender(<ChatMarkdown text={partial} />);
  }
  rerender(<ChatMarkdown text="**negrita** completa" />);
  expect(screen.getByText("negrita").tagName).toBe("STRONG");
});
