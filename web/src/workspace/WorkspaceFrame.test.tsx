import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import WorkspaceFrame, { type NarrowView, type WorkspaceFrameProps } from "./WorkspaceFrame";

const PAGE = "/subjects/historia/topics/revolucion-industrial";
const VIEWS: Record<NarrowView, string> = { document: "Documento", left: "Izquierda", chat: "Chat" };

function renderFrame(overrides: Partial<WorkspaceFrameProps> = {}) {
  const onView = vi.fn();
  const props: WorkspaceFrameProps = {
    mode: "study",
    subjectId: "historia",
    topicId: "revolucion-industrial",
    topicName: "La Revolución Industrial",
    label: "Pantalla",
    views: VIEWS,
    view: "left",
    onView,
    leftLabel: "Tarjeta izquierda",
    left: <p>Contenido izquierdo</p>,
    chat: <p>Contenido del chat</p>,
    document: <p>Contenido del documento</p>,
    ...overrides,
  };
  const { container } = render(<WorkspaceFrame {...props} />);
  return { onView, root: container.firstElementChild as HTMLElement };
}

it("renders the header band: the mode switch, the topic's name and the links on the right", () => {
  const { root } = renderFrame({ barEnd: <a href="/versiones">Versiones</a> });

  expect(root).toBe(screen.getByRole("main", { name: "Pantalla" }));
  expect(root).toHaveClass("workspace");
  expect(root).toHaveAttribute("data-mode", "study");
  const band = root.querySelector(".workspace-header .workspace-bar") as HTMLElement;
  const modes = within(band).getByRole("navigation", { name: "Modo del tema" });
  expect(within(modes).getByRole("link", { name: "Estudiar" })).toHaveAttribute("aria-current", "page");
  expect(within(modes).getByRole("link", { name: "Construir" })).toHaveAttribute("href", `${PAGE}/workspace`);
  const title = within(band).getByRole("heading", { level: 1, name: "La Revolución Industrial" });
  expect(within(title).getByRole("link")).toHaveAttribute("href", PAGE);
  const end = within(band).getByRole("navigation", { name: "Más" });
  expect(within(end).getAllByRole("link").map((link) => link.textContent)).toEqual(["Versiones", "Mesa de estudio"]);
});

it("puts the left card above the chat card in the left column and the document card on the right", () => {
  const { root } = renderFrame({ leftClassName: "mine", documentClassName: "doc-mine" });

  const columns = root.querySelector(".workspace-columns") as HTMLElement;
  const left = columns.querySelector(":scope > .workspace-left") as HTMLElement;
  const cards = [...left.children];
  expect(cards).toHaveLength(2);
  expect(cards[0]).toBe(screen.getByRole("region", { name: "Tarjeta izquierda" }));
  expect(cards[0]).toHaveClass("workspace-sources", "mine");
  expect(cards[0]).toHaveTextContent("Contenido izquierdo");
  expect(cards[1]).toBe(screen.getByRole("region", { name: "Chat" }));
  expect(cards[1]).toHaveClass("workspace-chat");
  expect(cards[1]).toHaveTextContent("Contenido del chat");
  const document = screen.getByRole("region", { name: "Documento" });
  expect(document.parentElement).toBe(columns);
  expect(document).toHaveClass("workspace-document", "doc-mine");
  expect(document).toHaveTextContent("Contenido del documento");
  expect(document).not.toHaveAttribute("data-panel");
  expect(columns.querySelector(".workspace-detail")).toBeNull();
  expect(root).not.toHaveAttribute("data-detail");
});

it("switches the single-column view in the order Documento, left, Chat", () => {
  const { onView, root } = renderFrame({ view: "chat" });

  const switcher = screen.getByRole("group", { name: "Qué mostrar" });
  const buttons = within(switcher).getAllByRole("button");
  expect(buttons.map((button) => button.textContent)).toEqual(["Documento", "Izquierda", "Chat"]);
  expect(buttons.map((button) => button.getAttribute("aria-pressed"))).toEqual(["false", "false", "true"]);
  expect(root).toHaveAttribute("data-view", "chat");

  fireEvent.click(buttons[0]);
  fireEvent.click(buttons[1]);

  expect(onView.mock.calls).toEqual([["document"], ["left"]]);
});

it("shows a detail over the document's cell and marks the panel of the document card", () => {
  const { root } = renderFrame({ detail: <aside>Detalle</aside>, documentPanel: true });

  const detail = root.querySelector(".workspace-columns > .workspace-detail") as HTMLElement;
  expect(detail).toHaveTextContent("Detalle");
  expect(root).toHaveAttribute("data-detail", "open");
  expect(screen.getByRole("region", { name: "Documento" })).toHaveAttribute("data-panel", "open");
  // The document stays mounted underneath.
  expect(screen.getByText("Contenido del documento")).toBeInTheDocument();
});

it("marks Construir as the current mode in build mode", () => {
  renderFrame({ mode: "build" });

  const modes = screen.getByRole("navigation", { name: "Modo del tema" });
  expect(within(modes).getByRole("link", { name: "Construir" })).toHaveAttribute("aria-current", "page");
  expect(within(modes).getByRole("link", { name: "Estudiar" })).not.toHaveAttribute("aria-current");
});
