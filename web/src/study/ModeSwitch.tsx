import { type MouseEvent, useState } from "react";
import { topicPath } from "../desk/api";
import { BUSY_MESSAGE, switchToStudy } from "./api";
import "./modeSwitch.css";

export type Mode = "build" | "study";

export const SWITCHING = "Pasando a Estudiar…";

function goTo(path: string) {
  window.location.assign(path);
}

/**
 * The header switch **Construir · Estudiar** (#333, #337, epic #332): **Construir** is a plain
 * link to the study workspace (`.../workspace`), where the notes stay editable. **Estudiar**, from
 * Construir, first switches the topic through the backend (`POST .../study`, #335: the capture
 * session is ended and the current notes labelled "versión de estudio") and only then opens the
 * study screen (`.../study`); while it runs it says "Pasando a Estudiar…". A refusal is said below
 * it in Spanish and nothing navigates: `409 notes_busy` asks to wait for the notes, anything else
 * gives the backend's `detail`. The current mode carries `aria-current="page"`. `navigate` is
 * where a successful switch goes (the browser's location by default; tests pass their own).
 */
export default function ModeSwitch({
  subjectId,
  topicId,
  current,
  navigate = goTo,
}: {
  subjectId: string;
  topicId: string;
  current: Mode;
  navigate?: (path: string) => void;
}) {
  const [switching, setSwitching] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const base = topicPath(subjectId, topicId);
  const study = `${base}/study`;

  const onStudy = (event: MouseEvent<HTMLAnchorElement>) => {
    if (current === "study") return;
    event.preventDefault();
    if (switching) return;
    setSwitching(true);
    setFailure(null);
    void switchToStudy(subjectId, topicId).then((result) => {
      if (result.kind === "ok") {
        navigate(study);
        return;
      }
      setSwitching(false);
      setFailure(result.kind === "busy" ? BUSY_MESSAGE : result.message);
    });
  };

  return (
    <div className="mode-switch-wrap">
      <nav className="mode-switch" aria-label="Modo del tema">
        <a href={`${base}/workspace`} aria-current={current === "build" ? "page" : undefined}>
          Construir
        </a>
        <a
          href={study}
          aria-current={current === "study" ? "page" : undefined}
          aria-disabled={switching || undefined}
          onClick={onStudy}
        >
          {switching ? SWITCHING : "Estudiar"}
        </a>
      </nav>
      {failure !== null && (
        <p className="mode-switch-failure" role="alert">
          {failure}
        </p>
      )}
    </div>
  );
}
