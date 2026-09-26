import { topicPath } from "../desk/api";
import "./modeSwitch.css";

export type Mode = "build" | "study";

/**
 * The header switch **Construir · Estudiar** (#333, epic #332): two links between the study
 * workspace (`.../workspace`, Construir) and the study screen (`.../study`, Estudiar); the current
 * one carries `aria-current="page"`. It only navigates: ending the capture and labelling the
 * "versión de estudio" on the way to Estudiar is #337.
 */
export default function ModeSwitch({ subjectId, topicId, current }: { subjectId: string; topicId: string; current: Mode }) {
  const base = topicPath(subjectId, topicId);
  const modes: Array<[Mode, string, string]> = [
    ["build", "Construir", `${base}/workspace`],
    ["study", "Estudiar", `${base}/study`],
  ];
  return (
    <nav className="mode-switch" aria-label="Modo del tema">
      {modes.map(([mode, label, href]) => (
        <a key={mode} href={href} aria-current={mode === current ? "page" : undefined}>
          {label}
        </a>
      ))}
    </nav>
  );
}
