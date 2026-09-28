import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import ReplyView from "./ReplyView";

const TEXT = "Mira el dibujo:\n\n![Imagen recortada 2](../sources/images/crop-002.png)\n";
const none = () => null;

it("draws an image of an answer when its path resolves (#509)", () => {
  render(
    <ReplyView
      text={TEXT}
      renderSection={none}
      renderSource={none}
      resolveImage={(src) => (src.startsWith("../sources/") ? `/api/x/${src.slice(11)}` : null)}
    />,
  );

  expect(screen.getByRole("img", { name: "Imagen recortada 2" })).toHaveAttribute("src", "/api/x/images/crop-002.png");
  expect(screen.queryByText("[Imagen: Imagen recortada 2]")).toBeNull();
});

it("keeps «[Imagen: …]» when the path does not resolve or there is no resolver", () => {
  const { rerender } = render(<ReplyView text={TEXT} renderSection={none} renderSource={none} resolveImage={none} />);
  expect(screen.getByText("[Imagen: Imagen recortada 2]")).toBeInTheDocument();

  rerender(<ReplyView text={TEXT} renderSection={none} renderSource={none} />);
  expect(screen.getByText("[Imagen: Imagen recortada 2]")).toBeInTheDocument();
  expect(screen.queryByRole("img")).toBeNull();
});
