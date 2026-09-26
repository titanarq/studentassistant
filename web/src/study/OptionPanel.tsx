import { type KeyboardEvent, type ReactNode, useEffect, useRef } from "react";

export const OPTION_PANEL_ID = "study-option-panel";

/**
 * The slide-over panel of the study screen (#333): it comes in over the right edge of the
 * document, which stays visible and scrollable at its left. A labelled region, not a modal: the
 * document stays reachable. Its heading takes the focus when it opens; "Cerrar" and Escape close
 * it (the page gives the focus back to the option button).
 */
export default function OptionPanel({
  title,
  onClose,
  hidden = false,
  children,
}: {
  title: string;
  onClose: () => void;
  /** Kept mounted but hidden (a source is shown over it), so a practice in course survives. */
  hidden?: boolean;
  children: ReactNode;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [title]);

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  return (
    <section
      id={OPTION_PANEL_ID}
      className="study-panel"
      aria-labelledby="study-panel-title"
      hidden={hidden}
      onKeyDown={onKeyDown}
    >
      <header className="study-panel-header">
        <h2 id="study-panel-title" tabIndex={-1} ref={headingRef}>
          {title}
        </h2>
        <button type="button" onClick={onClose}>
          Cerrar
        </button>
      </header>
      <div className="study-panel-body">{children}</div>
    </section>
  );
}
