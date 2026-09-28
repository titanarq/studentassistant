import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ConfirmOptions, ConfirmProvider, useConfirm } from "./ConfirmDialog";
import { TrashIcon } from "./icons";

const DELETE: ConfirmOptions = {
  title: "¿Borrar esta fuente?",
  message: <p>Saldrá de las fuentes del tema.</p>,
  confirmLabel: "Borrar",
  confirmIcon: <TrashIcon />,
  destructive: true,
};

/** A page with a button that asks `options` and writes down each answer. */
function Asker({ options, answers }: { options: ConfirmOptions; answers: boolean[] }) {
  const confirm = useConfirm();
  const [asked, setAsked] = useState(0);
  return (
    <main>
      <button type="button" onClick={() => void confirm(options).then((answer) => answers.push(answer))}>
        Abrir
      </button>
      <button type="button" onClick={() => setAsked((n) => n + 1)}>
        Otro {asked}
      </button>
    </main>
  );
}

function renderAsker(options: ConfirmOptions = DELETE) {
  const answers: boolean[] = [];
  render(<Asker options={options} answers={answers} />, { wrapper: ConfirmProvider });
  const opener = screen.getByRole("button", { name: "Abrir" });
  const open = () => {
    opener.focus();
    fireEvent.click(opener);
    return screen.getByRole("dialog", { name: options.title });
  };
  return { answers, opener, open };
}

afterEach(() => {
  vi.restoreAllMocks();
  document.documentElement.style.overflow = "";
});

describe("the confirmation modal (#486)", () => {
  it("opens as a modal dialog named by its title, described by its message, locking the page's scroll", () => {
    const showModal = vi.spyOn(HTMLDialogElement.prototype, "showModal");
    const { open } = renderAsker();
    expect(screen.queryByRole("dialog")).toBeNull();

    const dialog = open();
    expect(showModal).toHaveBeenCalledTimes(1);
    expect(dialog.tagName).toBe("DIALOG");
    expect(dialog).toHaveAttribute("open");
    expect(dialog).toHaveAccessibleName("¿Borrar esta fuente?");
    expect(dialog).toHaveAccessibleDescription("Saldrá de las fuentes del tema.");
    expect(within(dialog).getByRole("heading", { name: "¿Borrar esta fuente?" })).toBeInTheDocument();
    expect(dialog).toHaveClass("confirm-dialog-destructive");
    // Mounted at the provider, outside the page that asked.
    expect(screen.getByRole("main")).not.toContainElement(dialog);
    expect(document.documentElement.style.overflow).toBe("hidden");
  });

  it("shows the given labels and icons, and puts the focus on «Cancelar» for a destructive action", () => {
    const { open } = renderAsker();
    const dialog = open();
    const cancel = within(dialog).getByRole("button", { name: "Cancelar" });
    const confirm = within(dialog).getByRole("button", { name: "Borrar" });
    expect(cancel).toHaveFocus();
    expect(cancel.querySelector("svg")).not.toBeNull();
    expect(confirm.querySelector("svg")).not.toBeNull();
    expect(confirm).toHaveClass("confirm-dialog-confirm");
  });

  it("focuses the confirm button of a harmless question, with the default labels", () => {
    const { open } = renderAsker({ title: "¿Seguimos?" });
    const dialog = open();
    expect(dialog).not.toHaveClass("confirm-dialog-destructive");
    expect(dialog).not.toHaveAttribute("aria-describedby");
    expect(within(dialog).getByRole("button", { name: "Aceptar" })).toHaveFocus();
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
  });

  it("confirms: resolves true, closes, unlocks the scroll and gives the focus back to the opener", async () => {
    const { answers, opener, open } = renderAsker();
    const dialog = open();
    fireEvent.click(within(dialog).getByRole("button", { name: "Borrar" }));
    await waitFor(() => expect(answers).toEqual([true]));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(dialog).not.toHaveAttribute("open");
    expect(opener).toHaveFocus();
    expect(document.documentElement.style.overflow).toBe("");
  });

  it("cancels with «Cancelar»", async () => {
    const { answers, opener, open } = renderAsker();
    fireEvent.click(within(open()).getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(answers).toEqual([false]));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("cancels with Escape, and the key goes no further", async () => {
    const behind = vi.fn();
    window.addEventListener("keydown", behind);
    try {
      const { answers, opener, open } = renderAsker();
      const dialog = open();
      fireEvent.keyDown(within(dialog).getByRole("button", { name: "Cancelar" }), { key: "Escape" });
      await waitFor(() => expect(answers).toEqual([false]));
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(opener).toHaveFocus();
      expect(behind).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", behind);
    }
  });

  it("cancels on the browser's own cancel request", async () => {
    const { answers, open } = renderAsker();
    const dialog = open();
    const request = new Event("cancel", { cancelable: true });
    act(() => {
      dialog.dispatchEvent(request);
    });
    expect(request.defaultPrevented).toBe(true);
    await waitFor(() => expect(answers).toEqual([false]));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancels on a click on the backdrop, not on a click inside the card or a drag out of it", async () => {
    const { answers, opener, open } = renderAsker();
    const dialog = open();

    const message = within(dialog).getByText("Saldrá de las fuentes del tema.");
    fireEvent.pointerDown(message);
    fireEvent.click(message);
    // Pressed in the card, released on the backdrop: the click lands on the dialog, but no cancel.
    fireEvent.pointerDown(message);
    fireEvent.click(dialog);
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(answers).toEqual([]);

    fireEvent.pointerDown(dialog);
    fireEvent.click(dialog);
    await waitFor(() => expect(answers).toEqual([false]));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("answers an earlier question false when a new one is asked", async () => {
    const answers: string[] = [];
    function Twice() {
      const confirm = useConfirm();
      return (
        <button
          type="button"
          onClick={() => {
            void confirm({ title: "Primera" }).then((answer) => answers.push(`primera ${answer}`));
            void confirm({ title: "Segunda" }).then((answer) => answers.push(`segunda ${answer}`));
          }}
        >
          Preguntar
        </button>
      );
    }
    render(<Twice />, { wrapper: ConfirmProvider });
    fireEvent.click(screen.getByRole("button", { name: "Preguntar" }));
    await waitFor(() => expect(answers).toEqual(["primera false"]));
    const dialog = screen.getByRole("dialog", { name: "Segunda" });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole("button", { name: "Aceptar" }));
    await waitFor(() => expect(answers).toEqual(["primera false", "segunda true"]));
  });

  describe("with work to run on confirm", () => {
    it("stays open and busy while it runs, closing on success", async () => {
      let finish: (failure: string | null) => void = () => undefined;
      const { answers, open } = renderAsker({
        ...DELETE,
        busyLabel: "Borrando…",
        onConfirm: () => new Promise((resolve) => (finish = resolve)),
      });
      const dialog = open();
      fireEvent.click(within(dialog).getByRole("button", { name: "Borrar" }));

      expect(within(dialog).getByRole("button", { name: "Borrando…" })).toBeDisabled();
      expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeDisabled();
      expect(dialog).toHaveAttribute("aria-busy", "true");
      fireEvent.keyDown(dialog, { key: "Escape" });
      fireEvent.pointerDown(dialog);
      fireEvent.click(dialog);
      expect(screen.getByRole("dialog")).toBe(dialog);
      expect(answers).toEqual([]);

      act(() => finish(null));
      await waitFor(() => expect(answers).toEqual([true]));
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("shows a failure with only «Cerrar», which answers false", async () => {
      const { answers, opener, open } = renderAsker({ ...DELETE, onConfirm: async () => "No se pudo borrar: no." });
      const dialog = open();
      fireEvent.click(within(dialog).getByRole("button", { name: "Borrar" }));

      expect(await within(dialog).findByRole("alert")).toHaveTextContent("No se pudo borrar: no.");
      expect(within(dialog).queryByRole("button", { name: "Borrar" })).toBeNull();
      const close = within(dialog).getByRole("button", { name: "Cerrar" });
      expect(close).toHaveFocus();
      expect(dialog).toHaveAccessibleDescription("Saldrá de las fuentes del tema. No se pudo borrar: no.");
      fireEvent.click(close);
      await waitFor(() => expect(answers).toEqual([false]));
      expect(opener).toHaveFocus();
    });

    it("shows a thrown error as a failure too", async () => {
      const { open } = renderAsker({ ...DELETE, onConfirm: () => Promise.reject(new Error("Sin conexión.")) });
      const dialog = open();
      fireEvent.click(within(dialog).getByRole("button", { name: "Borrar" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Sin conexión.");
    });
  });

  it("refuses to be used without its provider", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => render(<Asker options={DELETE} answers={[]} />)).toThrow(/ConfirmProvider/);
  });
});
