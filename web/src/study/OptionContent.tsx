import ExamPage from "../exam/ExamPage";
import MaterialPreviewPage from "../materials/MaterialPreviewPage";
import PracticePage from "../practice/PracticePage";
import QuizPage from "../quiz/QuizPage";
import ExercisesView from "./ExercisesView";
import type { OptionKey, StudyOption } from "./options";

/** The file the outline generator writes under `generated/`. */
const OUTLINE_FILE = "esquema.md";

export const NOT_GENERATED = "Todavía no está generado.";

/** What to say in the study chat to get each option generated. */
export const CHAT_REQUESTS: Record<OptionKey, string> = {
  esquema: "hazme un esquema",
  ejercicios: "hazme ejercicios",
  examen: "hazme un examen",
  quiz: "hazme un quiz",
  tarjetas: "hazme tarjetas de memoria",
};

/** «Pídelo en el chat: "hazme un quiz"» (again, for a stale one). */
export function chatHint(option: StudyOption): string {
  const again = option.state === "stale" ? " de nuevo" : "";
  return `Pídelo${again} en el chat: «${CHAT_REQUESTS[option.key]}».`;
}

/**
 * What an open option shows. The page has no "Generar" button (human decision on #333, epic
 * #332: everything the student asks goes through the chat): an option never generated says
 * "Todavía no está generado." and what to ask the study chat (#336); a stale one gives its reason
 * and the same hint above the material; else the existing page, embedded, reporting the anchors
 * of the item shown (`onFocusAnchors`).
 */
export default function OptionContent({
  subjectId,
  topicId,
  option,
  onFocusAnchors,
  anchorLabel,
}: {
  subjectId: string;
  topicId: string;
  option: StudyOption;
  onFocusAnchors: (anchors: string[]) => void;
  anchorLabel: (anchor: string) => string;
}) {
  if (option.state === "missing") {
    return (
      <div className="study-missing">
        <p>{NOT_GENERATED}</p>
        <p className="study-note">{chatHint(option)}</p>
      </div>
    );
  }
  const props = { subjectId, topicId, embedded: true, onFocusAnchors };
  return (
    <>
      {option.state === "stale" && (
        <div className="study-stale" role="note">
          <p>
            <span className="badge badge-warn">Desactualizado</span> {option.staleReason}
          </p>
          <p className="study-note">{chatHint(option)}</p>
        </div>
      )}
      {option.key === "esquema" && (
        <MaterialPreviewPage subjectId={subjectId} topicId={topicId} name={OUTLINE_FILE} embedded />
      )}
      {option.key === "ejercicios" && (
        <ExercisesView subjectId={subjectId} topicId={topicId} onFocusAnchors={onFocusAnchors} anchorLabel={anchorLabel} />
      )}
      {option.key === "examen" && <ExamPage {...props} />}
      {option.key === "quiz" && <QuizPage {...props} />}
      {option.key === "tarjetas" && <PracticePage {...props} anchorLabel={anchorLabel} />}
    </>
  );
}
