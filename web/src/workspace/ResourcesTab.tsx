import { useEffect, useMemo, useState } from "react";
import { describeFailure, fetchTopicSummary, type ReadResult, type TopicSummary } from "../desk/api";
import type { NotesTree } from "../notes/markdown";
import SourcePanel from "../notes/SourcePanel";
import { resourceList } from "./resources";

/** The source open in the tab: a footnote label and its definition. */
export interface OpenResource {
  label: string;
  definition: string | undefined;
}

export interface ResourcesTabProps {
  subjectId: string;
  topicId: string;
  tree: NotesTree | null;
  /** Bumped each time the tab is shown, so the counts are read again. */
  refreshKey: number;
  open: OpenResource | null;
  onOpen: (resource: OpenResource) => void;
  onClose: () => void;
}

/**
 * The **Recursos** tab (#312): the topic's sources grouped by kind (`resourceList`) and, once
 * one is chosen (here or from a footnote of the document), the sources viewer (`SourcePanel`) in
 * its place; "Cerrar" goes back to the list.
 */
export default function ResourcesTab({ subjectId, topicId, tree, refreshKey, open, onOpen, onClose }: ResourcesTabProps) {
  const [summary, setSummary] = useState<ReadResult<TopicSummary> | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchTopicSummary(subjectId, topicId).then((result) => {
      if (!cancelled) setSummary(result);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, refreshKey]);

  const counts = summary?.kind === "ok" ? summary.value.sources : null;
  const list = useMemo(() => resourceList(counts, tree), [counts, tree]);

  if (open !== null) {
    return (
      <div className="workspace-resources">
        <SourcePanel subjectId={subjectId} topicId={topicId} label={open.label} definition={open.definition} onClose={onClose} />
      </div>
    );
  }

  return (
    <div className="workspace-resources">
      {summary === null && <p>Cargando las fuentes…</p>}
      {summary !== null && summary.kind !== "ok" && (
        <p role="alert">No se pudieron cargar las fuentes del tema: {describeFailure(summary)}</p>
      )}
      {summary !== null && list.groups.length === 0 && list.uncitedWebs === 0 && (
        <p>Este tema todavía no tiene fuentes: captura páginas en la pestaña Captura.</p>
      )}
      {list.groups.map((group) => (
        <section key={group.kind} className="workspace-resource-group" aria-label={group.title}>
          <h3>
            {group.title} <span className="badge">{group.items.length}</span>
          </h3>
          <ul>
            {group.items.map((item) => (
              <li key={item.key}>
                <button
                  type="button"
                  className="workspace-resource"
                  onClick={() => onOpen({ label: item.label, definition: item.definition })}
                >
                  {item.title}
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {list.uncitedWebs > 0 && (
        <p className="workspace-resources-note">
          {list.uncitedWebs === 1
            ? "Hay 1 web guardada que los apuntes todavía no citan."
            : `Hay ${list.uncitedWebs} webs guardadas que los apuntes todavía no citan.`}
        </p>
      )}
    </div>
  );
}
