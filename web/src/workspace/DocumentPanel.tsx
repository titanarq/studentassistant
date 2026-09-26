import { useCallback, useMemo, useRef, useState } from "react";
import { notesImageUrl, saveNotes, uploadPastedImage } from "../notes/api";
import { type NotesTree, parseNotes } from "../notes/markdown";
import NotesView from "../notes/NotesView";
import NoteEditor, { type NoteEditorHandle, type UploadResult } from "../noteEditor/NoteEditor";
import { useWorkspace } from "./state";

/**
 * The document column of the workspace (#312, #316): the topic's `apuntes.md` read-only with an
 * **Editar** button, or the notes editor. Saving sends the whole text with the revision the edit
 * started from (`PUT .../notes`, epic #311): a stale base shows both versions and lets the student
 * discard their changes or keep them on the current version (nothing is overwritten silently);
 * busy notes and format errors are explained in Spanish. While editing, a newer revision of the
 * notes (the assistant changed them) is announced without touching the student's text.
 */

export const CHANGED_WHILE_EDITING = "Los apuntes han cambiado mientras editabas";
export const BUSY = "Se están preparando los apuntes; espera a que terminen.";
export const ASSISTANT_CHANGED = "El asistente ha cambiado los apuntes";
export const EMPTY_NOTES = "Todavía no hay apuntes: pídeselos al asistente en el chat.";

interface Base {
  text: string;
  revision: string | null;
  version: number | null;
}

type Problem =
  | { kind: "changed"; text: string; revision: string | null; mine: string }
  | { kind: "busy" }
  | { kind: "invalid"; errors: string[] }
  | { kind: "failed"; message: string };

export interface DocumentPanelProps {
  topicName: string;
  tree: NotesTree | null;
  onOpenSource: (label: string, trigger: HTMLElement) => void;
  activeLabel: string | null;
}

export default function DocumentPanel({ topicName, tree, onOpenSource, activeLabel }: DocumentPanelProps) {
  const { subjectId, topicId, notes, changedSections, reloadNotes } = useWorkspace();
  const [base, setBase] = useState<Base | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [saving, setSaving] = useState(false);
  const [retried, setRetried] = useState(false);
  const dirty = useRef(false);
  const editor = useRef<NoteEditorHandle | null>(null);

  const resolveImage = useCallback((src: string) => notesImageUrl(subjectId, topicId, src), [subjectId, topicId]);
  const noop = useCallback(() => undefined, []);
  const preview = useCallback(
    (text: string) => <NotesView tree={parseNotes(text)} onOpenSource={noop} resolveImage={resolveImage} />,
    [noop, resolveImage],
  );
  const upload = useCallback(
    async (file: File): Promise<UploadResult> => {
      const result = await uploadPastedImage(subjectId, topicId, file);
      return result.kind === "ok" ? { kind: "ok", markdown: result.markdown } : result;
    },
    [subjectId, topicId],
  );

  const startEditing = () => {
    if (notes.kind === "ready") setBase({ text: notes.text, revision: notes.revision, version: notes.version });
    else if (notes.kind === "empty") setBase({ text: `# ${topicName}\n`, revision: null, version: null });
    else return;
    dirty.current = false;
    setProblem(null);
    setRetried(false);
  };

  const stopEditing = () => {
    setBase(null);
    setProblem(null);
    setRetried(false);
  };

  const cancel = () => {
    if (dirty.current && !window.confirm("¿Descartar los cambios que no has guardado?")) return;
    stopEditing();
  };

  const save = async () => {
    if (base === null || editor.current === null || saving) return;
    const text = editor.current.getText();
    setSaving(true);
    setProblem(null);
    const result = await saveNotes(subjectId, topicId, text, base.revision);
    setSaving(false);
    switch (result.kind) {
      case "saved":
        stopEditing();
        await reloadNotes(result.notesChanged ? result.changedSections : undefined);
        return;
      case "changed":
        setProblem({ kind: "changed", text: result.text, revision: result.revision, mine: text });
        return;
      default:
        setProblem(result);
    }
  };

  const discard = async () => {
    stopEditing();
    await reloadNotes();
  };

  /** Keeps the student's text in the editor, now on the current version. */
  const retryOnCurrent = () => {
    if (base === null || problem?.kind !== "changed") return;
    setBase({ ...base, text: problem.text, revision: problem.revision });
    setProblem(null);
    setRetried(true);
    void reloadNotes();
  };

  const newer = base !== null && notes.kind === "ready" && notes.revision !== null && notes.revision !== base.revision;
  const conflictTrees = useMemo(
    () => (problem?.kind === "changed" ? { mine: parseNotes(problem.mine), current: parseNotes(problem.text) } : null),
    [problem],
  );

  if (base !== null) {
    return (
      <div className="workspace-editing">
        {newer && problem === null && (
          <p className="workspace-banner" role="status">
            {ASSISTANT_CHANGED}. Tu texto sigue como lo dejaste; al guardar se comprobará.
          </p>
        )}
        {problem !== null && <ProblemView problem={problem} trees={conflictTrees} onDiscard={discard} onRetry={retryOnCurrent} resolveImage={resolveImage} />}
        {retried && problem === null && (
          <p className="workspace-banner" role="status">
            Tu texto sigue en el editor, ahora sobre la versión actual: revísalo y vuelve a guardar.
          </p>
        )}
        <NoteEditor
          ref={editor}
          initialText={base.text}
          uploadImage={upload}
          resolveImage={(src) => resolveImage(src) ?? ""}
          renderPreview={preview}
          onChange={() => {
            dirty.current = true;
          }}
          leading={
            <>
              <h2 className="workspace-document-title">Apuntes</h2>
              <span className="workspace-editing-chip">
                {base.version !== null ? `Editando sobre v${base.version}` : base.revision === null ? "Apuntes nuevos" : "Editando"}
              </span>
            </>
          }
          actions={
            <>
              <button type="button" className="workspace-cancel" onClick={cancel} disabled={saving}>
                Cancelar
              </button>
              <button type="button" className="primary" onClick={() => void save()} disabled={saving}>
                {saving ? "Guardando…" : "Guardar"}
              </button>
            </>
          }
        />
      </div>
    );
  }

  return (
    <>
      <div className="workspace-document-header">
        <h2 className="workspace-document-title">Apuntes</h2>
        {notes.kind === "ready" && notes.version !== null && (
          <span className="workspace-document-version">v{notes.version} · guardado</span>
        )}
        <div className="note-editor-spacer" />
        {(notes.kind === "ready" || notes.kind === "empty") && (
          <button type="button" className="workspace-edit" onClick={startEditing}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M4 20h4L19 9l-4-4L4 16v4z" />
              <path d="M13 7l4 4" />
            </svg>
            Editar
          </button>
        )}
      </div>
      {notes.kind === "loading" && <p>Cargando los apuntes…</p>}
      {notes.kind === "empty" && <p className="workspace-empty">{EMPTY_NOTES}</p>}
      {notes.kind === "failed" && <p role="alert">No se pudieron cargar los apuntes: {notes.message}</p>}
      {tree !== null && (
        <NotesView
          tree={tree}
          onOpenSource={onOpenSource}
          activeLabel={activeLabel}
          changedSections={changedSections}
          resolveImage={resolveImage}
        />
      )}
    </>
  );
}

function ProblemView({
  problem,
  trees,
  onDiscard,
  onRetry,
  resolveImage,
}: {
  problem: Problem;
  trees: { mine: NotesTree; current: NotesTree } | null;
  onDiscard: () => void;
  onRetry: () => void;
  resolveImage: (src: string) => string | null;
}) {
  const noop = () => undefined;
  switch (problem.kind) {
    case "busy":
      return (
        <p className="workspace-problem" role="alert">
          {BUSY}
        </p>
      );
    case "failed":
      return (
        <p className="workspace-problem" role="alert">
          No se pudieron guardar los apuntes: {problem.message}
        </p>
      );
    case "invalid":
      return (
        <div className="workspace-problem" role="alert">
          <p>Los apuntes no se han guardado porque no cumplen el formato:</p>
          <ul>
            {problem.errors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        </div>
      );
    case "changed":
      return (
        <section className="workspace-problem workspace-conflict" role="alert" aria-label={CHANGED_WHILE_EDITING}>
          <p className="workspace-conflict-title">{CHANGED_WHILE_EDITING}</p>
          <p>Tus cambios no se han guardado. Compara las dos versiones y elige:</p>
          <div className="workspace-conflict-actions">
            <button type="button" onClick={onDiscard}>
              Descartar mis cambios
            </button>
            <button type="button" className="primary" onClick={onRetry}>
              Reintentar sobre la versión actual
            </button>
          </div>
          {trees !== null && (
            <div className="workspace-conflict-versions">
              <div aria-label="Tu versión">
                <h3>Tu versión</h3>
                <NotesView tree={trees.mine} onOpenSource={noop} resolveImage={resolveImage} />
              </div>
              <div aria-label="Versión actual">
                <h3>Versión actual</h3>
                <NotesView tree={trees.current} onOpenSource={noop} resolveImage={resolveImage} />
              </div>
            </div>
          )}
        </section>
      );
  }
}
