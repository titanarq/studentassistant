/**
 * The study workspace's shared state (#312, epic #311): the topic's document as last read
 * (`GET /api/subjects/{s}/topics/{t}/notes`) with its `revision`, and `reloadNotes()`, which the
 * chat calls after a turn changed the notes. Direct editing (#316) and the live chat panel (#317)
 * read and refresh the document through this module only.
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { describeFailure } from "../desk/api";
import { fetchNotes } from "../notes/api";

export type WorkspaceNotes =
  | { kind: "loading" }
  /** `revision` is null while the backend does not send it (before #313). */
  | { kind: "ready"; text: string; revision: string | null; version: number | null }
  /** The topic has no `apuntes.md` yet (404). */
  | { kind: "empty" }
  | { kind: "failed"; message: string };

export interface WorkspaceState {
  subjectId: string;
  topicId: string;
  notes: WorkspaceNotes;
  /** Anchors of the sections the last change touched, highlighted in the document. */
  changedSections: ReadonlySet<string>;
  /** Reads the notes again; `changedSections` replaces the highlighted sections when given. */
  reloadNotes: (changedSections?: string[]) => Promise<void>;
}

/**
 * Holds the notes of one topic. Only the latest read is kept, so an older answer never
 * overwrites newer notes.
 */
export function useWorkspaceState(subjectId: string, topicId: string): WorkspaceState {
  const [notes, setNotes] = useState<WorkspaceNotes>({ kind: "loading" });
  const [changedSections, setChanged] = useState<ReadonlySet<string>>(new Set());
  const reads = useRef(0);

  const reloadNotes = useCallback(
    async (changed?: string[]) => {
      if (changed !== undefined) setChanged(new Set(changed));
      const read = ++reads.current;
      const result = await fetchNotes(subjectId, topicId);
      if (read !== reads.current) return;
      if (result.kind === "ok") {
        const { text, version, revision } = result.value;
        setNotes({ kind: "ready", text, version, revision: revision ?? null });
      } else if (result.kind === "not-found") {
        setNotes({ kind: "empty" });
      } else {
        setNotes({ kind: "failed", message: describeFailure(result) });
      }
    },
    [subjectId, topicId],
  );

  useEffect(() => {
    void reloadNotes();
    return () => {
      reads.current++;
    };
  }, [reloadNotes]);

  return { subjectId, topicId, notes, changedSections, reloadNotes };
}

export const WorkspaceContext = createContext<WorkspaceState | null>(null);

/** The workspace state of the enclosing `WorkspacePage`. */
export function useWorkspace(): WorkspaceState {
  const state = useContext(WorkspaceContext);
  if (state === null) throw new Error("useWorkspace outside a WorkspacePage");
  return state;
}
