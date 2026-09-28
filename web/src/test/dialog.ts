/**
 * jsdom has `HTMLDialogElement` but none of its behaviour (#486): no `show()`, `showModal()` or
 * `close()`, no `cancel` on Escape. This installs the part of the HTML spec the app relies on, so
 * a test drives a `<dialog>` the way a browser would:
 * - `showModal()` sets `open` (and throws `InvalidStateError` on a non-modal open dialog, like a
 *   browser); `show()` sets `open`;
 * - `close(returnValue?)` removes `open`, sets `returnValue` and fires `close` (in a task, as the
 *   spec queues it);
 * - an Escape `keydown` while a modal dialog is open fires a cancelable `cancel` on the topmost
 *   one, and closes it unless the handler called `preventDefault()`.
 * What it cannot give is the top layer: the page behind is not inert in jsdom.
 */

const modals: HTMLDialogElement[] = [];

function onEscape(event: KeyboardEvent) {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  const top = modals[modals.length - 1];
  if (top === undefined) return;
  const cancel = new Event("cancel", { cancelable: true });
  if (top.dispatchEvent(cancel)) top.close();
}

export function installDialogPolyfill(): void {
  // A suite in the node environment (`@vitest-environment node`) has no DOM at all.
  if (typeof HTMLDialogElement === "undefined") return;
  const proto = HTMLDialogElement.prototype as HTMLDialogElement & Record<string, unknown>;
  if (typeof proto.showModal === "function") return;

  Object.defineProperty(proto, "returnValue", { configurable: true, writable: true, value: "" });

  proto.show = function show(this: HTMLDialogElement) {
    if (this.open) return;
    this.setAttribute("open", "");
  };

  proto.showModal = function showModal(this: HTMLDialogElement) {
    if (this.open) {
      if (modals.includes(this)) return;
      throw new DOMException("The dialog is already open as a non-modal dialog.", "InvalidStateError");
    }
    if (!this.isConnected) throw new DOMException("The dialog is not connected.", "InvalidStateError");
    this.setAttribute("open", "");
    modals.push(this);
    if (modals.length === 1) document.addEventListener("keydown", onEscape);
  };

  proto.close = function close(this: HTMLDialogElement, returnValue?: string) {
    if (!this.open) return;
    this.removeAttribute("open");
    if (returnValue !== undefined) this.returnValue = returnValue;
    const index = modals.indexOf(this);
    if (index >= 0) {
      modals.splice(index, 1);
      if (modals.length === 0) document.removeEventListener("keydown", onEscape);
    }
    setTimeout(() => this.dispatchEvent(new Event("close")), 0);
  };
}
