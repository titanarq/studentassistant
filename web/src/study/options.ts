/**
 * The study options of the study screen (#333) and their state, from `GET .../study` (#335,
 * `api.ts`): **Esquema** (kind `esquema`), **Ejercicios** (the `exercises` of kind `examen`),
 * **Examen** (its `questions`), **Quiz** (kind `quiz`) and **Tarjetas de memoria** (the practice
 * queue, kind `flashcards`) and **Diapositivas** (kind `diapositivas`, #382). The backend says whether each is "Listo", "Desactualizado" (with its
 * reason) or "Sin generar"; an option it does not mention is "Sin generar".
 */

import type { StudyOptionState, StudyState } from "./api";

export type OptionKey = "esquema" | "ejercicios" | "examen" | "quiz" | "tarjetas" | "diapositivas";
export type OptionState = StudyOptionState;

export interface StudyOptionInfo {
  key: OptionKey;
  title: string;
  /** The generator kind of the material the option shows. */
  kind: string;
}

export interface StudyOption extends StudyOptionInfo {
  state: OptionState;
  staleReason: string | null;
}

export const STUDY_OPTIONS: readonly StudyOptionInfo[] = [
  { key: "esquema", title: "Esquema", kind: "esquema" },
  { key: "ejercicios", title: "Ejercicios", kind: "examen" },
  { key: "examen", title: "Examen", kind: "examen" },
  { key: "quiz", title: "Quiz", kind: "quiz" },
  { key: "tarjetas", title: "Tarjetas de memoria", kind: "flashcards" },
  {
    key: "diapositivas",
    title: "Diapositivas",
   
    kind: "diapositivas",
  },
];

export const STATE_LABELS: Record<OptionState, string> = {
  ready: "Listo",
  stale: "Desactualizado",
  missing: "Sin generar",
};

export const STALE_FALLBACK = "Los apuntes han cambiado desde que se generó.";

/** The options in the study order, each with its state (`null` state: all missing). */
export function studyOptions(study: StudyState | null): StudyOption[] {
  return STUDY_OPTIONS.map((info) => {
    const status = study?.options.find((option) => option.key === info.key);
    const state = status?.state ?? "missing";
    return { ...info, state, staleReason: state === "stale" ? (status?.staleReason ?? STALE_FALLBACK) : null };
  });
}
