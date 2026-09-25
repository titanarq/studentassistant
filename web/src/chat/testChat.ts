/** Answers of the editor chat API for the tests. */

export const DIFF = `--- apuntes.md (antes)
+++ apuntes.md
@@ -5,2 +5,3 @@
 ## 1. Contexto {#contexto}
-Resumen corto.
+La Revolución Industrial empezó en Gran Bretaña a mediados del siglo XVIII.
+Por ejemplo, la fábrica textil de Manchester.
`;

export function revision(overrides: Record<string, unknown> = {}) {
  return {
    subject: "historia",
    topic: "revolucion-industrial",
    message: "Esto está demasiado resumido",
    reply: "He ampliado el contexto con tu página 1 y he añadido un ejemplo.",
    applied: true,
    summary: "Contexto ampliado con un ejemplo",
    ops: [],
    footnotes: [],
    fidelity_mode: null,
    style_rules: [],
    notes_changed: true,
    changed_sections: ["contexto"],
    diff: DIFF,
    notes: "...",
    paths: ["subjects/historia/topics/revolucion-industrial/notes/apuntes.md"],
    commit: "abc123",
    attempts: 1,
    errors: [],
    warning: null,
    model: "claude-opus",
    ...overrides,
  };
}

export function history(turns: Record<string, unknown>[] = [], canUndo = false) {
  return { subject: "historia", topic: "revolucion-industrial", turns, can_undo: canUndo };
}

export function turn(overrides: Record<string, unknown> = {}) {
  return {
    time: "2026-09-25T10:00:00Z",
    message: "Pon un ejemplo",
    reply: "Añadido un ejemplo.",
    applied: true,
    summary: "Ejemplo añadido",
    changed_sections: ["causas"],
    commit: "old111",
    undone: false,
    warning: null,
    ...overrides,
  };
}
