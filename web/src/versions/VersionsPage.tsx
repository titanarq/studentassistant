import { useCallback, useEffect, useRef, useState } from "react";
import { fetchTopics, topicPath } from "../desk/api";
import { type ActionResult, describeActionFailure } from "../pending/doubts";
import {
  fetchVersionDiff,
  fetchVersions,
  type NotesVersions,
  restoreVersion,
  type RestoreResult,
  type VersionDiff,
} from "./api";
import VersionDiffView, { type DiffLayout, versionName } from "./VersionDiffView";
import "./versions.css";

/**
 * `/subjects/<subject>/topics/<topic>/versions`: the history of a topic's notes (#72, over the
 * versions API of #64). It lists every version, newest first, with its date and message, marks
 * the one the current notes are, and says when the notes changed after the latest version. Two
 * versions (or a version and the current notes) are compared by section (`VersionDiffView`),
 * inline or side by side. "Restaurar" writes a version back as a new one, after a confirmation;
 * the list and the comparison are read again afterwards.
 */

/** `null` in `to` is the current notes. */
export interface Comparison {
  from: number;
  to: number | null;
}

/** What to compare first: the current notes with the latest version when they differ from it,
 * otherwise the latest version with the one before; `null` when there is nothing to compare. */
export function defaultComparison(list: NotesVersions): Comparison | null {
  const versions = list.versions;
  if (versions.length === 0) return null;
  const latest = versions[versions.length - 1].version;
  if (list.has_notes && list.changed_since_latest) return { from: latest, to: null };
  if (versions.length < 2) return null;
  return { from: versions[versions.length - 2].version, to: latest };
}

const DATE_FORMAT = new Intl.DateTimeFormat("es-ES", { dateStyle: "long", timeStyle: "short" });

export function formatTaggedAt(taggedAt: string | null): string | null {
  if (taggedAt === null) return null;
  const date = new Date(taggedAt);
  return Number.isNaN(date.getTime()) ? null : DATE_FORMAT.format(date);
}

function restoredMessage(result: RestoreResult): string {
  return `Se ha restaurado la versión ${result.restored_version} como versión ${result.version}.`;
}

export default function VersionsPage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [topicName, setTopicName] = useState(topicId);
  const [list, setList] = useState<ActionResult<NotesVersions> | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [diff, setDiff] = useState<ActionResult<VersionDiff> | null>(null);
  const [layout, setLayout] = useState<DiffLayout>("inline");
  const [confirming, setConfirming] = useState<number | null>(null);
  const [restoring, setRestoring] = useState(false);
  const [restored, setRestored] = useState<ActionResult<RestoreResult> | null>(null);
  const reads = useRef(0);
  const diffReads = useRef(0);

  // Only the latest read is shown, so an older answer never overwrites a newer one.
  const loadList = useCallback(async () => {
    const read = ++reads.current;
    const result = await fetchVersions(subjectId, topicId);
    if (read !== reads.current) return;
    setList(result);
    setComparison(result.kind === "ok" ? defaultComparison(result.value) : null);
  }, [subjectId, topicId]);

  useEffect(() => {
    let cancelled = false;
    fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (!cancelled && topic) setTopicName(topic.name);
    });
    void loadList();
    return () => {
      cancelled = true;
      reads.current++;
    };
  }, [subjectId, topicId, loadList]);

  useEffect(() => {
    const read = ++diffReads.current;
    setDiff(null);
    if (comparison === null) return;
    fetchVersionDiff(subjectId, topicId, comparison.from, comparison.to).then((result) => {
      if (read === diffReads.current) setDiff(result);
    });
    return () => {
      diffReads.current++;
    };
  }, [subjectId, topicId, comparison]);

  const restore = async (version: number) => {
    setConfirming(null);
    setRestoring(true);
    setRestored(null);
    const result = await restoreVersion(subjectId, topicId, version);
    setRestored(result);
    setRestoring(false);
    if (result.kind === "ok") void loadList();
  };

  const versions = list?.kind === "ok" ? list.value : null;
  const newestFirst = versions ? [...versions.versions].reverse() : [];
  const latest = newestFirst[0]?.version;

  return (
    <main className="versions-page">
      <p>
        <a href={topicPath(subjectId, topicId)}>← Tema {topicName}</a>
      </p>
      <h1>Versiones de los apuntes de {topicName}</h1>
      {list === null && <p>Cargando las versiones…</p>}
      {list !== null && list.kind !== "ok" && (
        <p role="alert">No se pudieron cargar las versiones: {describeActionFailure(list)}</p>
      )}
      <div role="status">
        {restored?.kind === "ok" && (
          <>
            <p>{restoredMessage(restored.value)}</p>
            {restored.value.warning !== null && <p className="versions-warning">{restored.value.warning}</p>}
          </>
        )}
      </div>
      {restored !== null && restored.kind !== "ok" && (
        <p role="alert">No se pudo restaurar la versión: {describeActionFailure(restored)}</p>
      )}
      {versions !== null && versions.versions.length === 0 && (
        <p>Todavía no hay versiones de los apuntes de este tema. Se crea una cada vez que el editor prepara el tema.</p>
      )}
      {versions !== null && versions.versions.length > 0 && (
        <>
          <section aria-labelledby="versions-list-heading">
            <h2 id="versions-list-heading">Historial</h2>
            {versions.changed_since_latest && (
              <p>Los apuntes actuales tienen cambios posteriores a la versión {latest}.</p>
            )}
            {!versions.has_notes && <p>Ahora mismo el tema no tiene apuntes.</p>}
            <ol className="versions-list" reversed>
              {newestFirst.map((entry) => {
                const when = formatTaggedAt(entry.tagged_at);
                return (
                  <li key={entry.version} aria-label={versionName(entry.version)}>
                    <strong>{versionName(entry.version)}</strong>
                    {entry.current && <span className="versions-current"> · la de los apuntes actuales</span>}
                    {when !== null && <span className="versions-date"> · {when}</span>}
                    {entry.message !== "" && <p className="versions-message">{entry.message}</p>}
                    {!entry.current && confirming !== entry.version && (
                      <button type="button" disabled={restoring} onClick={() => setConfirming(entry.version)}>
                        Restaurar la versión {entry.version}
                      </button>
                    )}
                    {confirming === entry.version && (
                      <div role="group" aria-label={`Confirmar la restauración de la versión ${entry.version}`}>
                        <p>
                          Los apuntes actuales pasarán a ser los de la versión {entry.version}, guardados como una
                          versión nueva. No se pierde ninguna versión.
                          {versions.changed_since_latest &&
                            ` Los cambios hechos después de la versión ${latest} no tienen versión propia.`}
                        </p>
                        <button type="button" onClick={() => void restore(entry.version)}>
                          Sí, restaurar
                        </button>{" "}
                        <button type="button" onClick={() => setConfirming(null)}>
                          Cancelar
                        </button>
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
            {restoring && <p>Restaurando…</p>}
          </section>
          <section aria-labelledby="versions-compare-heading">
            <h2 id="versions-compare-heading">Comparar</h2>
            {comparison === null ? (
              <p>Solo hay una versión y los apuntes no han cambiado desde ella: todavía no hay nada que comparar.</p>
            ) : (
              <CompareForm
                versions={versions}
                comparison={comparison}
                onChange={setComparison}
                layout={layout}
                onLayout={setLayout}
              />
            )}
            {comparison !== null && diff === null && <p>Comparando…</p>}
            {diff !== null && diff.kind !== "ok" && (
              <p role="alert">No se pudieron comparar las versiones: {describeActionFailure(diff)}</p>
            )}
            {diff?.kind === "ok" && <VersionDiffView diff={diff.value} layout={layout} />}
          </section>
        </>
      )}
    </main>
  );
}

function CompareForm({
  versions,
  comparison,
  onChange,
  layout,
  onLayout,
}: {
  versions: NotesVersions;
  comparison: Comparison;
  onChange: (comparison: Comparison) => void;
  layout: DiffLayout;
  onLayout: (layout: DiffLayout) => void;
}) {
  const numbers = versions.versions.map((v) => v.version).reverse();
  return (
    <form className="versions-compare" aria-label="Comparar versiones" onSubmit={(e) => e.preventDefault()}>
      <label>
        Desde{" "}
        <select
          value={comparison.from}
          onChange={(e) => onChange({ ...comparison, from: Number(e.target.value) })}
        >
          {numbers.map((n) => (
            <option key={n} value={n}>
              {versionName(n)}
            </option>
          ))}
        </select>
      </label>{" "}
      <label>
        Hasta{" "}
        <select
          value={comparison.to === null ? "current" : comparison.to}
          onChange={(e) => onChange({ ...comparison, to: e.target.value === "current" ? null : Number(e.target.value) })}
        >
          {versions.has_notes && <option value="current">{versionName(null)}</option>}
          {numbers.map((n) => (
            <option key={n} value={n}>
              {versionName(n)}
            </option>
          ))}
        </select>
      </label>
      <fieldset>
        <legend>Ver los cambios</legend>
        <label>
          <input type="radio" name="layout" checked={layout === "inline"} onChange={() => onLayout("inline")} /> En línea
        </label>{" "}
        <label>
          <input type="radio" name="layout" checked={layout === "side"} onChange={() => onLayout("side")} /> Lado a lado
        </label>
      </fieldset>
    </form>
  );
}
