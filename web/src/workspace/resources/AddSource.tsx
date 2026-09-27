import { useState } from "react";
import BookTitleForm from "../../topic/BookTitleForm";
import PdfUploadForm from "../../topic/PdfUploadForm";
import WebPageForm from "../../topic/WebPageForm";

/** Which part of the control is shown: only the button, the three choices, or one form. */
type Step = "closed" | "choosing" | "pdf" | "web" | "book";

const CHOICES: { step: Exclude<Step, "closed" | "choosing">; label: string }[] = [
  { step: "pdf", label: "PDF" },
  { step: "web", label: "Página web" },
  { step: "book", label: "Libro de texto" },
];

/**
 * «Añadir fuente» in the workspace's **Recursos** tab (#384): one button that opens a small
 * inline panel with three choices -- **PDF**, **Página web**, **Libro de texto** -- each showing
 * the topic page's existing form (`PdfUploadForm`, `WebPageForm`, `BookTitleForm`). A successful
 * add closes the panel, leaves a short confirmation and calls `onAdded` so the tab re-reads its
 * source list; a failure stays in the form, shown as the form shows it. **Cancelar** closes it.
 */
export default function AddSource({
  subjectId,
  topicId,
  onAdded,
}: {
  subjectId: string;
  topicId: string;
  onAdded: () => void;
}) {
  const [step, setStep] = useState<Step>("closed");
  const [done, setDone] = useState<string | null>(null);

  function finish(message: string) {
    setStep("closed");
    setDone(message);
    onAdded();
  }

  if (step === "closed") {
    return (
      <div className="add-source">
        <button
          type="button"
          onClick={() => {
            setDone(null);
            setStep("choosing");
          }}
        >
          Añadir fuente
        </button>
        {done !== null && (
          <p role="status" className="add-source-done">
            {done}
          </p>
        )}
      </div>
    );
  }

  return (
    <section className="add-source add-source-panel" aria-label="Añadir fuente">
      <div className="add-source-choices" role="group" aria-label="Tipo de fuente">
        {CHOICES.map((choice) => (
          <button
            key={choice.step}
            type="button"
            aria-pressed={step === choice.step}
            onClick={() => setStep(choice.step)}
          >
            {choice.label}
          </button>
        ))}
        <button type="button" onClick={() => setStep("closed")}>
          Cancelar
        </button>
      </div>
      {step === "pdf" && (
        <PdfUploadForm
          subjectId={subjectId}
          topicId={topicId}
          onImported={(imported) => finish(`PDF «${imported.original_name}» añadido.`)}
        />
      )}
      {step === "web" && (
        <WebPageForm
          subjectId={subjectId}
          topicId={topicId}
          onAdded={(added) => finish(`Página «${added.title}» guardada como fuente.`)}
        />
      )}
      {step === "book" && (
        <BookTitleForm
          subjectId={subjectId}
          topicId={topicId}
          onSaved={(title) => finish(title === null ? "Título del libro borrado." : `Libro de texto: «${title}».`)}
        />
      )}
    </section>
  );
}
