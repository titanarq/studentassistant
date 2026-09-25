/** Backend bodies of the study materials API, as `GET .../generated` answers them. */

export const TOPIC = "/api/subjects/historia/topics/revolucion-francesa";
export const GENERATED = `${TOPIC}/generated`;
const ROOT = "subjects/historia/topics/revolucion-francesa/generated";

function meta(kind: string, files: string[], extra: Record<string, unknown> = {}) {
  return {
    kind,
    generator_version: 1,
    built_at: "2026-09-24T18:30:00Z",
    model: "claude-test",
    prompt_hash: "abc",
    options: {},
    notes: { sha256: "f".repeat(64), version: 3, tag: "historia/revolucion-francesa/apuntes-v3", changed_since_version: false },
    files,
    items: [],
    unresolved: [],
    warnings: [],
    ...extra,
  };
}

export function artifact(kind: string, title: string | null, files: string[] = [], extra: Record<string, unknown> = {}) {
  const generated = files.length > 0;
  return {
    kind,
    title,
    generated,
    stale: false,
    stale_reason: null,
    files: generated ? [...files.map((f) => `${ROOT}/${f}`), `${ROOT}/${kind}.meta.yaml`] : [],
    meta: generated ? meta(kind, files) : null,
    ...extra,
  };
}

export function materialsBody(artifacts: unknown[], hasNotes = true) {
  return {
    subject: "historia",
    topic: "revolucion-francesa",
    has_notes: hasNotes,
    notes_sha256: hasNotes ? "f".repeat(64) : null,
    artifacts,
  };
}

export const GENERATORS = [
  { kind: "diapositivas", title: "Diapositivas", description: "Una presentación del tema.", version: 1, options_schema: {} },
  { kind: "esquema", title: "Esquema", description: "Esquema jerárquico del tema con un mapa mental.", version: 1, options_schema: {} },
];
