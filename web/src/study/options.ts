/**
 * The study options of the study screen (#333) and their state, from `GET .../generated`
 * (`materials/api.ts`): **Esquema** (kind `esquema`), **Ejercicios** (the `exercises` of kind
 * `examen`), **Examen** (its `questions`), **Quiz** (kind `quiz`) and **Tarjetas de memoria** (the
 * practice queue, kind `flashcards`). A kind generated and not stale is "Listo", a stale one
 * "Desactualizado" (with the backend's reason), anything else "Sin generar".
 */

import type { Artifact, Materials } from "../materials/api";

export type OptionKey = "esquema" | "ejercicios" | "examen" | "quiz" | "tarjetas";
export type OptionState = "ready" | "stale" | "missing";

export interface StudyOptionInfo {
  key: OptionKey;
  title: string;
  description: string;
  /** The generator kind whose state the option shows and which "Generar" runs. */
  kind: string;
}

export interface StudyOption extends StudyOptionInfo {
  state: OptionState;
  staleReason: string | null;
  /** The generated files of the kind (names under `generated/`). */
  files: string[];
}

export const STUDY_OPTIONS: readonly StudyOptionInfo[] = [
  { key: "esquema", title: "Esquema", description: "El tema en un esquema jerárquico", kind: "esquema" },
  { key: "ejercicios", title: "Ejercicios", description: "Uno a uno, con su solución", kind: "examen" },
  { key: "examen", title: "Examen", description: "Simulacro con criterios de corrección", kind: "examen" },
  { key: "quiz", title: "Quiz", description: "Preguntas para comprobar lo que sabes", kind: "quiz" },
  { key: "tarjetas", title: "Tarjetas de memoria", description: "Repaso espaciado", kind: "flashcards" },
];

export const STATE_LABELS: Record<OptionState, string> = {
  ready: "Listo",
  stale: "Desactualizado",
  missing: "Sin generar",
};

export const STALE_FALLBACK = "Los apuntes han cambiado desde que se generó.";

function stateOf(artifact: Artifact | undefined): OptionState {
  if (artifact === undefined || !artifact.generated) return "missing";
  return artifact.stale ? "stale" : "ready";
}

/** The options in the study order, each with the state of its kind (`null` materials: all missing). */
export function studyOptions(materials: Materials | null): StudyOption[] {
  return STUDY_OPTIONS.map((info) => {
    const artifact = materials?.artifacts.find((a) => a.kind === info.kind);
    const state = stateOf(artifact);
    return {
      ...info,
      state,
      staleReason: state === "stale" ? (artifact?.staleReason ?? STALE_FALLBACK) : null,
      files: artifact?.files ?? [],
    };
  });
}
