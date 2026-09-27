import { useEffect, useState } from "react";
import { fetchMaterials, fileUrl } from "../materials/api";
import MaterialPreviewPage from "../materials/MaterialPreviewPage";

/** The files the slides generator writes under `generated/` (`generators/slides.py`). */
export const SLIDES_MARKDOWN = "diapositivas.md";
const EXPORTS: ReadonlyArray<{ name: string; label: string }> = [
  { name: "diapositivas.pdf", label: "Descargar PDF" },
  { name: "diapositivas.pptx", label: "Descargar PowerPoint" },
];

/**
 * «Diapositivas» on the study screen (#382): the preview of the deck the topic card page shows
 * (`MaterialPreviewPage`, embedded) with **Descargar PDF** and **Descargar PowerPoint**, the
 * exports `GET .../generated` lists for kind `diapositivas`. Until that read answers both links are
 * offered; once it answers, an export it does not list (Marp could not produce it) says so instead
 * of linking a missing file. The preview reports no anchors, so no section of the document is
 * highlighted.
 */
export default function SlidesView({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [files, setFiles] = useState<string[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchMaterials(subjectId, topicId).then((result) => {
      if (cancelled || result.kind !== "ok") return;
      setFiles(result.value.artifacts.find((a) => a.kind === "diapositivas")?.files ?? []);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  return (
    <div className="study-slides">
      <ul className="study-slides-downloads">
        {EXPORTS.map(({ name, label }) => (
          <li key={name}>
            {files === null || files.includes(name) ? (
              <a href={fileUrl(subjectId, topicId, name)} download>
                {label}
              </a>
            ) : (
              <span className="study-note">{label.replace("Descargar ", "")}: no se pudo exportar.</span>
            )}
          </li>
        ))}
      </ul>
      <MaterialPreviewPage subjectId={subjectId} topicId={topicId} name={SLIDES_MARKDOWN} embedded />
    </div>
  );
}
