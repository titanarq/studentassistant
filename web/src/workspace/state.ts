/**
 * The study workspace's shared state (#312, epic #311): the topic's document as last read
 * (`GET /api/subjects/{s}/topics/{t}/notes`) with its `revision`, and `reloadNotes()`, which the
 * chat calls after a turn changed the notes, and `doubtsChanged()`, which it calls when a doubt
 * is asked or resolved. Direct editing (#316) and the live chat panel (#317)
 * read and refresh the document through this module only.
 *
 * Since #516 it also holds the open doubts marked in the notes (`GET .../doubts/marks`), read again
 * on every doubt event (`doubtsChanged`) and every new revision of the notes, and `showDoubt` /
 * `showNextDoubt`, which bring one doubt to the chat (`POST .../doubts/{id}/ask`, a badge clicked
 * or «Siguiente duda.»).
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { describeFailure } from "../desk/api";
import { fetchNotes } from "../notes/api";
import { describeActionFailure } from "../pending/doubts";
import { askDoubt, type DoubtMarks, fetchDoubtMarks, nextDoubt } from "./doubtMarks";

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
  /** Bumped when the chat hears a doubt asked, resolved or marked: the marks are read again. */
  doubtsKey: number;
  doubtsChanged: () => void;
  /** The open doubts marked in the notes (#516); `null` until read (or when it failed). */
  doubtMarks: DoubtMarks | null;
  /** Shows that doubt in the chat; a refusal is kept in `doubtProblem`. */
  showDoubt: (pendingId: string) => Promise<void>;
  /** «Siguiente duda.»: the first marked doubt not asked yet. */
  showNextDoubt: () => Promise<void>;
  /** A doubt is being brought to the chat. */
  showingDoubt: boolean;
  /** Why the last doubt could not be shown (Spanish), until the next try. */
  doubtProblem: string | null;
}

/**
 * Holds the notes of one topic. Only the latest read is kept, so an older answer never
 * overwrites newer notes.
 */
export function useWorkspaceState(subjectId: string, topicId: string): WorkspaceState {
  const [notes, setNotes] = useState<WorkspaceNotes>({ kind: "loading" });
  const [changedSections, setChanged] = useState<ReadonlySet<string>>(new Set());
  const reads = useRef(0);
  const [doubtsKey, setDoubtsKey] = useState(0);
  const doubtsChanged = useCallback(() => setDoubtsKey((n) => n + 1), []);

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

  const [doubtMarks, setDoubtMarks] = useState<DoubtMarks | null>(null);
  const marksRef = useRef<DoubtMarks | null>(null);
  marksRef.current = doubtMarks;
  const markReads = useRef(0);
  const revision = notes.kind === "ready" ? (notes.revision ?? notes.text) : notes.kind;
  useEffect(() => {
    const read = ++markReads.current;
    void fetchDoubtMarks(subjectId, topicId).then((result) => {
      if (read !== markReads.current) return;
      setDoubtMarks(result.kind === "ok" ? result.value : null);
    });
  }, [subjectId, topicId, doubtsKey, revision]);

  const [showingDoubt, setShowing] = useState(false);
  const [doubtProblem, setDoubtProblem] = useState<string | null>(null);
  const showDoubt = useCallback(
    async (pendingId: string) => {
      setShowing(true);
      setDoubtProblem(null);
      const result = await askDoubt(subjectId, topicId, pendingId);
      setShowing(false);
      if (result.kind !== "ok") setDoubtProblem(`No se pudo mostrar la duda: ${describeActionFailure(result)}`);
      // Asked now (or closed, unknown): the marks are read again.
      setDoubtsKey((n) => n + 1);
    },
    [subjectId, topicId],
  );
  const showNextDoubt = useCallback(async () => {
    const next = nextDoubt(marksRef.current);
    if (next !== null) await showDoubt(next);
  }, [showDoubt]);

  return {
    subjectId,
    topicId,
    notes,
    changedSections,
    reloadNotes,
    doubtsKey,
    doubtsChanged,
    doubtMarks,
    showDoubt,
    showNextDoubt,
    showingDoubt,
    doubtProblem,
  };
}

export const WorkspaceContext = createContext<WorkspaceState | null>(null);

/** The workspace state of the enclosing `WorkspacePage`. */
export function useWorkspace(): WorkspaceState {
  const state = useContext(WorkspaceContext);
  if (state === null) throw new Error("useWorkspace outside a WorkspacePage");
  return state;
}
