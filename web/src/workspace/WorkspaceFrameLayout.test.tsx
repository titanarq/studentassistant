import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import WorkspaceFrame, { type NarrowView, type WorkspaceFrameProps } from "./WorkspaceFrame";

// #534: expandable chat, collapsible left card, draggable divider (shared by Construir and Estudiar).

const VIEWS: Record<NarrowView, string> = { document: "Documento", left: "Izquierda", chat: "Chat" };
const KEY = "studentassistant.workspace.sidePercent";

beforeEach(() => {
  window.sessionStorage.clear();
});

function renderFrame(overrides: Partial<WorkspaceFrameProps> = {}) {
  const props: WorkspaceFrameProps = {
    mode: "study",
    subjectId: "historia",
    topicId: "revolucion-industrial",
    topicName: "La Revolución Industrial",
    label: "Pantalla",
    views: VIEWS,
    view: "left",
    onView: vi.fn(),
    leftLabel: "Tarjeta izquierda",
    left: <p>Contenido izquierdo</p>,
    chat: <p>Contenido del chat</p>,
    document: <p>Contenido del documento</p>,
    ...overrides,
  };
  const { container } = render(<WorkspaceFrame {...props} />);
  return { root: container.firstElementChild as HTMLElement };
}

function side(root: HTMLElement): string {
  return (root.querySelector(".workspace-columns") as HTMLElement).style.getPropertyValue("--workspace-side");
}

function box(left: number, width: number) {
  return () => ({ left, width, top: 0, height: 600, right: left + width, bottom: 600, x: left, y: 0, toJSON: () => ({}) });
}

it("expands the chat over the left card and reduces it again", () => {
  const { root } = renderFrame();
  const leftCard = screen.getByRole("region", { name: "Tarjeta izquierda" });

  expect(root).not.toHaveAttribute("data-chat");
  fireEvent.click(screen.getByRole("button", { name: "Ampliar chat" }));
  expect(root).toHaveAttribute("data-chat", "expanded");
  // The card stays mounted (a running capture goes on); only the chat's controls remain.
  expect(leftCard).toHaveTextContent("Contenido izquierdo");
  expect(screen.queryByRole("button", { name: /^Ocultar/ })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Reducir chat" }));
  expect(root).not.toHaveAttribute("data-chat");
  expect(screen.getByRole("button", { name: "Ampliar chat" })).toBeInTheDocument();
});

it("collapses and expands the left card on its own", () => {
  renderFrame();
  const leftCard = screen.getByRole("region", { name: "Tarjeta izquierda" });
  const toggle = screen.getByRole("button", { name: "Ocultar tarjeta izquierda" });

  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(leftCard).not.toHaveAttribute("data-collapsed");
  fireEvent.click(toggle);
  expect(leftCard).toHaveAttribute("data-collapsed", "true");
  const shown = screen.getByRole("button", { name: "Mostrar tarjeta izquierda" });
  expect(shown).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(shown);
  expect(leftCard).not.toHaveAttribute("data-collapsed");
});

it("lets the host own the collapsed state", () => {
  const onLeftCollapsed = vi.fn();
  renderFrame({ leftCollapsed: true, onLeftCollapsed });

  expect(screen.getByRole("region", { name: "Tarjeta izquierda" })).toHaveAttribute("data-collapsed", "true");
  fireEvent.click(screen.getByRole("button", { name: "Mostrar tarjeta izquierda" }));
  expect(onLeftCollapsed).toHaveBeenCalledWith(false);
});

it("gives the chat column 40 % by default and moves the divider with the keyboard, keeping the width", () => {
  const { root } = renderFrame();
  const divider = screen.getByRole("separator", { name: "Ancho del chat" });

  expect(side(root)).toBe("40%");
  expect(divider).toHaveAttribute("aria-valuenow", "40");
  expect(divider).toHaveAttribute("tabindex", "0");
  fireEvent.keyDown(divider, { key: "ArrowRight" });
  expect(side(root)).toBe("42%");
  fireEvent.keyDown(divider, { key: "ArrowLeft", shiftKey: true });
  expect(side(root)).toBe("32%");
  expect(window.sessionStorage.getItem(KEY)).toBe("32");
  fireEvent.keyDown(divider, { key: "Home" });
  expect(side(root)).toBe("25%");
  fireEvent.keyDown(divider, { key: "ArrowLeft" });
  expect(side(root)).toBe("25%");
  fireEvent.keyDown(divider, { key: "End" });
  expect(side(root)).toBe("65%");
  fireEvent.keyDown(divider, { key: "ArrowRight", shiftKey: true });
  expect(side(root)).toBe("65%");
});

it("restores the width of the session on the next render", () => {
  window.sessionStorage.setItem(KEY, "55");

  expect(side(renderFrame().root)).toBe("55%");
});

it("ignores a broken stored width and clamps an out-of-range one", () => {
  window.sessionStorage.setItem(KEY, "abc");
  expect(side(renderFrame().root)).toBe("40%");
  cleanup();
  window.sessionStorage.setItem(KEY, "99");
  expect(side(renderFrame().root)).toBe("65%");
});

it("works when sessionStorage throws", () => {
  const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  try {
    const { root } = renderFrame();
    expect(side(root)).toBe("40%");
    fireEvent.keyDown(screen.getByRole("separator", { name: "Ancho del chat" }), { key: "ArrowRight" });
    expect(side(root)).toBe("42%");
  } finally {
    get.mockRestore();
    set.mockRestore();
  }
});

it("drags the divider with the pointer within the limits and stores the width", () => {
  const { root } = renderFrame();
  const divider = screen.getByRole("separator", { name: "Ancho del chat" });
  (root.querySelector(".workspace-columns") as HTMLElement).getBoundingClientRect = box(0, 1000);
  divider.getBoundingClientRect = box(0, 16);

  fireEvent.pointerDown(divider, { button: 0, clientX: 400, pointerId: 1 });
  fireEvent.pointerMove(divider, { clientX: 500, pointerId: 1 });
  expect(side(root)).toBe("49.2%");
  fireEvent.pointerMove(divider, { clientX: 5, pointerId: 1 });
  expect(side(root)).toBe("25%");
  fireEvent.pointerMove(divider, { clientX: 990, pointerId: 1 });
  expect(side(root)).toBe("65%");
  fireEvent.pointerMove(divider, { clientX: 300, pointerId: 1 });
  fireEvent.pointerUp(divider, { pointerId: 1 });
  expect(side(root)).toBe("29.2%");
  expect(window.sessionStorage.getItem(KEY)).toBe("29.2");
  // After the drop, moving the pointer does nothing.
  fireEvent.pointerMove(divider, { clientX: 600, pointerId: 1 });
  expect(side(root)).toBe("29.2%");
});
