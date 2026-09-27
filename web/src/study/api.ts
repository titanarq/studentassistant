/**
 * Client of the switch to Estudiar (#335, `docs/modules/server.md`, "Switch to Estudiar"):
 * - `GET /api/subjects/{s}/topics/{t}/study` -> `StudyState`: the notes version labelled
 *   "versión de estudio" (or none), whether the notes still equal it (`study_current`) and each
 *   study option's state (`listo`, `desactualizado` with its reason, `sin_generar`).
 * - `POST .../study`, no body -> the same `StudyState` once the topic's capture session is ended
 *   and the current notes are labelled. `409 notes_busy` while the notes are being prepared or an
 *   editor turn runs; another 409 (no notes) carries the backend's Spanish `detail`.
 *
 * Bodies are read leniently: an option the page does not know is skipped, a body without the
 * fields the page needs is an error.
 */

import { getJson, type ReadResult, topicPath } from "../desk/api";
import { errorCode } from "../protocol/errors";
import type { OptionKey } from "./options";

/** The study states of the backend, as the page names them. */
export type StudyOptionState = "ready" | "stale" | "missing";

export interface StudyOptionStatus {
  key: OptionKey;
  state: StudyOptionState;
  /** Spanish, only when `stale` (null: the backend gave none). */
  staleReason: string | null;
}

export interface StudyLabel {
  /** The `N` of `apuntes-vN`. */
  version: number;
  tag: string;
}

export interface StudyState {
  studyVersion: StudyLabel | null;
  /** The current notes are still exactly the labelled version. */
  studyCurrent: boolean;
  options: StudyOptionStatus[];
}

const KEYS: readonly OptionKey[] = ["esquema", "ejercicios", "examen", "quiz", "tarjetas", "diapositivas"];
const STATES: Record<string, StudyOptionState> = { listo: "ready", desactualizado: "stale", sin_generar: "missing" };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readOption(entry: unknown): StudyOptionStatus | null {
  if (!isRecord(entry)) return null;
  const key = KEYS.find((k) => k === entry.key);
  const state = typeof entry.state === "string" ? STATES[entry.state] : undefined;
  if (key === undefined || state === undefined) return null;
  const reason = typeof entry.stale_reason === "string" && entry.stale_reason !== "" ? entry.stale_reason : null;
  return { key, state, staleReason: state === "stale" ? reason : null };
}

function readLabel(value: unknown): StudyLabel | null {
  if (!isRecord(value)) return null;
  const { version, tag } = value;
  if (typeof version !== "number" || !Number.isInteger(version) || version < 1) return null;
  return { version, tag: typeof tag === "string" ? tag : "" };
}

/** A `StudyState` body, or null when it lacks what the page needs. */
export function readStudyState(body: unknown): StudyState | null {
  if (!isRecord(body) || !Array.isArray(body.options)) return null;
  return {
    studyVersion: readLabel(body.study_version),
    studyCurrent: body.study_current === true,
    options: body.options.flatMap((entry) => {
      const option = readOption(entry);
      return option === null ? [] : [option];
    }),
  };
}

function decodeStudyState(value: unknown): StudyState {
  const state = readStudyState(value);
  if (state === null) throw new Error("not a StudyState");
  return state;
}

export function studyApiPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/study`;
}

export function fetchStudyState(subjectId: string, topicId: string): Promise<ReadResult<StudyState>> {
  return getJson(studyApiPath(subjectId, topicId), decodeStudyState);
}

/** What a switch to Estudiar (`POST .../study`) came to. */
export type SwitchResult =
  | { kind: "ok"; state: StudyState }
  /** `409 notes_busy`: the notes are being prepared, or an editor turn runs. */
  | { kind: "busy" }
  /** Anything else (no notes, unknown topic, vault, network): one Spanish sentence. */
  | { kind: "failed"; message: string };

export const BUSY_MESSAGE = "Se están preparando los apuntes; espera a que terminen.";
export const UNREACHABLE = "No se pudo conectar con el servidor.";

export async function switchToStudy(subjectId: string, topicId: string): Promise<SwitchResult> {
  let response: Response;
  try {
    response = await fetch(studyApiPath(subjectId, topicId), { method: "POST" });
  } catch {
    return { kind: "failed", message: UNREACHABLE };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  const fallback = `El servidor respondió con un error (${response.status}).`;
  if (response.ok) {
    const state = readStudyState(body);
    return state === null ? { kind: "failed", message: fallback } : { kind: "ok", state };
  }
  if (response.status === 409 && errorCode(body) === "notes_busy") return { kind: "busy" };
  const detail = isRecord(body) && typeof body.detail === "string" && body.detail !== "" ? body.detail : fallback;
  return { kind: "failed", message: detail };
}
