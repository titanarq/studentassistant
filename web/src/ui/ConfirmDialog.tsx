import {
  createContext,
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent,
  type ReactNode,
  type SyntheticEvent,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { CheckIcon, CloseIcon, QuestionIcon, WarningIcon } from "./icons";
import "./confirmDialog.css";

/**
 * The app's one confirmation (#486): a full-screen modal -- the whole viewport dimmed behind a
 * centred card with an icon, a title, an optional message and two buttons -- used for every
 * question "¿seguro?" instead of `window.confirm` or an inline confirmation. Ask it with
 * `useConfirm()` under the `ConfirmProvider` mounted once at the app root (`main.tsx`).
 */
export interface ConfirmOptions {
  /** The question, also the dialog's accessible name («¿Borrar esta fuente?»). */
  title: string;
  /** What happens if the student confirms; the dialog's description. */
  message?: ReactNode;
  /** The confirm button's label (default «Aceptar»). */
  confirmLabel?: string;
  /** The cancel button's label (default «Cancelar»). */
  cancelLabel?: string;
  /** The confirm button's icon (default a tick). */
  confirmIcon?: ReactNode;
  /** The card's icon beside the title (default a warning when destructive, a question otherwise). */
  icon?: ReactNode;
  /** A destructive action: the confirm button in red-pen colours and the focus on «Cancelar». */
  destructive?: boolean;
  /**
   * Optional work to run on confirm while the dialog stays open, its confirm button showing
   * `busyLabel` and nothing able to close it. It answers null when done (the dialog closes and
   * the promise resolves true) or a Spanish sentence saying why it failed: the dialog shows it,
   * keeps only a «Cerrar» button, and resolves false once closed.
   */
  onConfirm?: () => Promise<string | null>;
  /** The confirm button's label while `onConfirm` runs (default «Un momento…»). */
  busyLabel?: string;
}

/** Asks for a confirmation; resolves true when confirmed, false when cancelled or closed. */
export type Confirm = (options: ConfirmOptions) => Promise<boolean>;

type Phase = { kind: "asking" } | { kind: "running" } | { kind: "failed"; message: string };

export interface ConfirmDialogProps {
  options: ConfirmOptions;
  /** Called once with the answer; the host then unmounts the dialog. */
  onClose: (confirmed: boolean) => void;
}

/** Locks the page's scroll while a modal is open (the counter lets two overlap safely). */
let scrollLocks = 0;
let savedOverflow = "";

function lockScroll(): () => void {
  const root = document.documentElement;
  if (scrollLocks === 0) {
    savedOverflow = root.style.overflow;
    root.style.overflow = "hidden";
  }
  scrollLocks += 1;
  return () => {
    scrollLocks -= 1;
    if (scrollLocks === 0) root.style.overflow = savedOverflow;
  };
}

/**
 * The modal itself: a native `<dialog>` opened with `showModal()`, so the browser dims and makes
 * inert everything behind it and keeps Tab inside. Escape (the dialog's `cancel`) and a click on
 * the backdrop cancel; the focus starts on «Cancelar» for a destructive action (on the confirm
 * button otherwise) and goes back to the element that had it when the dialog closes.
 */
export function ConfirmDialog({ options, onClose }: ConfirmDialogProps) {
  const { title, message, destructive = false, onConfirm } = options;
  const dialog = useRef<HTMLDialogElement | null>(null);
  const cancelButton = useRef<HTMLButtonElement | null>(null);
  const confirmButton = useRef<HTMLButtonElement | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "asking" });
  const titleId = useId();
  const messageId = useId();
  const failureId = useId();
  const closed = useRef(false);
  // A press that started on the backdrop, so a drag out of the card does not cancel.
  const pressedBackdrop = useRef(false);

  const finish = useCallback(
    (confirmed: boolean) => {
      if (closed.current) return;
      closed.current = true;
      onClose(confirmed);
    },
    [onClose],
  );

  // Open as a modal on mount; on unmount close it, unlock the scroll and give the focus back.
  useLayoutEffect(() => {
    const node = dialog.current;
    if (node === null) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const unlock = lockScroll();
    if (!node.open) node.showModal();
    (destructive ? cancelButton.current : confirmButton.current)?.focus();
    return () => {
      if (node.open) node.close();
      unlock();
      if (opener !== null && opener.isConnected) opener.focus();
    };
    // Opened once per dialog: the host mounts a new one for each question.
  }, []);

  const running = phase.kind === "running";
  const failed = phase.kind === "failed";

  // Once a failure is shown only «Cerrar» is left: it takes the focus.
  useEffect(() => {
    if (failed) cancelButton.current?.focus();
  }, [failed]);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const confirm = () => {
    if (phase.kind !== "asking") return;
    if (onConfirm === undefined) {
      finish(true);
      return;
    }
    setPhase({ kind: "running" });
    void onConfirm().then(
      (failure) => {
        if (!mounted.current) return;
        if (failure === null) finish(true);
        else setPhase({ kind: "failed", message: failure });
      },
      (error: unknown) => {
        if (!mounted.current) return;
        setPhase({ kind: "failed", message: error instanceof Error ? error.message : String(error) });
      },
    );
  };

  const cancel = () => {
    if (!running) finish(false);
  };

  // Escape is the dialog's own: taken here, so nothing behind it (a panel that closes on Escape)
  // sees it and the browser does not close the dialog itself; the host unmounts it.
  const onKeyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    cancel();
  };

  // Any other close request the browser turns into `cancel` (a back gesture): the same answer.
  const onCancel = (event: SyntheticEvent<HTMLDialogElement>) => {
    event.preventDefault();
    cancel();
  };

  const onPointerDown = (event: PointerEvent<HTMLDialogElement>) => {
    pressedBackdrop.current = event.target === event.currentTarget;
  };
  // The dialog box is the card's own size, so a click whose target is the dialog itself is on
  // the backdrop around it.
  const onClick = (event: MouseEvent<HTMLDialogElement>) => {
    const onBackdrop = event.target === event.currentTarget;
    const started = pressedBackdrop.current;
    pressedBackdrop.current = false;
    if (onBackdrop && started) cancel();
  };

  const icon = options.icon ?? (destructive ? <WarningIcon size={22} /> : <QuestionIcon size={22} />);
  const described = [message !== undefined ? messageId : null, failed ? failureId : null].filter((id) => id !== null).join(" ");

  return (
    <dialog
      ref={dialog}
      className={destructive ? "confirm-dialog confirm-dialog-destructive" : "confirm-dialog"}
      aria-labelledby={titleId}
      aria-describedby={described === "" ? undefined : described}
      aria-busy={running || undefined}
      onKeyDown={onKeyDown}
      onCancel={onCancel}
      // Closed by the browser itself (a forced close request): the same as cancelling.
      onClose={() => finish(false)}
      onPointerDown={onPointerDown}
      onClick={onClick}
    >
      <div className="confirm-dialog-card">
        <div className="confirm-dialog-head">
          <span className="confirm-dialog-icon" aria-hidden="true">
            {icon}
          </span>
          <h2 id={titleId} className="confirm-dialog-title">
            {title}
          </h2>
        </div>
        {message !== undefined && (
          <div id={messageId} className="confirm-dialog-message">
            {message}
          </div>
        )}
        {failed && (
          <p id={failureId} className="confirm-dialog-failure" role="alert">
            {phase.message}
          </p>
        )}
        <div className="confirm-dialog-actions">
          <button ref={cancelButton} type="button" className="confirm-dialog-cancel" onClick={cancel} disabled={running}>
            <CloseIcon />
            {failed ? "Cerrar" : (options.cancelLabel ?? "Cancelar")}
          </button>
          {!failed && (
            <button ref={confirmButton} type="button" className="confirm-dialog-confirm" onClick={confirm} disabled={running}>
              {options.confirmIcon ?? <CheckIcon />}
              {running ? (options.busyLabel ?? "Un momento…") : (options.confirmLabel ?? "Aceptar")}
            </button>
          )}
        </div>
      </div>
    </dialog>
  );
}

const ConfirmContext = createContext<Confirm | null>(null);

interface Request {
  id: number;
  options: ConfirmOptions;
  resolve: (confirmed: boolean) => void;
}

/**
 * Holds the one confirmation on screen for everything under it. Mounted once at the app root
 * (`main.tsx`); a test renders its component under it too (`render(ui, {wrapper: ConfirmProvider})`).
 * A second question while one is open answers the first with false.
 */
export function ConfirmProvider({ children }: { children?: ReactNode }) {
  const [request, setRequest] = useState<Request | null>(null);
  const current = useRef<Request | null>(null);
  const nextId = useRef(0);

  const confirm = useCallback<Confirm>(
    (options) =>
      new Promise<boolean>((resolve) => {
        current.current?.resolve(false);
        nextId.current += 1;
        const next = { id: nextId.current, options, resolve };
        current.current = next;
        setRequest(next);
      }),
    [],
  );

  useEffect(
    () => () => {
      current.current?.resolve(false);
      current.current = null;
    },
    [],
  );

  const close = useCallback((confirmed: boolean) => {
    const answered = current.current;
    if (answered === null) return;
    current.current = null;
    setRequest(null);
    answered.resolve(confirmed);
  }, []);

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {request !== null && <ConfirmDialog key={request.id} options={request.options} onClose={close} />}
    </ConfirmContext.Provider>
  );
}

/** The app's confirmation: `await confirm({title, ...})` is true when the student confirmed. */
export function useConfirm(): Confirm {
  const confirm = useContext(ConfirmContext);
  if (confirm === null) throw new Error("useConfirm() needs a <ConfirmProvider> above it (main.tsx mounts one)");
  return confirm;
}
