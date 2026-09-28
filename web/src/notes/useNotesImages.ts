import { useCallback } from "react";
import { notesImageUrl } from "./api";

/**
 * The image resolver every view of a topic's notes passes to `NotesView` (#509): a
 * `../sources/<kind>/<file>` link (a pasted image, an «Imagen recortada N») is read through the
 * sources API; anything else stays the «[Imagen: …]» placeholder. The notes and the generated
 * material both sit one level below the topic, so the same relative path resolves for both.
 */
export function useNotesImages(subjectId: string, topicId: string): (src: string) => string | null {
  return useCallback((src: string) => notesImageUrl(subjectId, topicId, src), [subjectId, topicId]);
}
