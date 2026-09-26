import { type FormEvent, useEffect, useRef, useState } from "react";
import { describeFailure, fetchSubjects, type ReadResult } from "../desk/api";
import { describeActionFailure } from "../pending/doubts";
import { fetchStyleGuide, MAX_RULE_CHARS, MAX_RULES, sameRule, saveStyleGuide, type StyleGuide } from "./api";
import "./styleGuide.css";

/**
 * `/subjects/<subject>/style-guide`: the subject's style guide (#216, over the API of #70), the
 * general preferences the editor follows in every topic of the subject ("usa tablas para
 * comparar"). The student edits a rule, deletes one or adds one; each change writes the whole
 * list (`PUT .../style-guide`) and the page shows the list the backend answers.
 */

type Change = { kind: "saved"; text: string } | { kind: "failed"; text: string };

export default function StyleGuidePage({ subjectId }: { subjectId: string }) {
  const [subjectName, setSubjectName] = useState(subjectId);
  const [guide, setGuide] = useState<ReadResult<StyleGuide> | null>(null);
  const [editing, setEditing] = useState<{ index: number; text: string } | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [change, setChange] = useState<Change | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    void (async () => {
      const subjects = await fetchSubjects();
      if (!mounted.current || subjects.kind !== "ok") return;
      const subject = subjects.value.find((candidate) => candidate.subject_id === subjectId);
      if (subject !== undefined) setSubjectName(subject.name);
    })();
    void (async () => {
      const result = await fetchStyleGuide(subjectId);
      if (mounted.current) setGuide(result);
    })();
    return () => {
      mounted.current = false;
    };
  }, [subjectId]);

  const rules = guide?.kind === "ok" ? guide.value.rules : [];

  const save = async (next: string[], done: string) => {
    if (saving) return false;
    setSaving(true);
    setChange(null);
    const result = await saveStyleGuide(subjectId, next);
    if (!mounted.current) return false;
    setSaving(false);
    if (result.kind !== "ok") {
      setChange({ kind: "failed", text: `No se pudo guardar la guía de estilo: ${describeActionFailure(result)}` });
      return false;
    }
    setGuide({ kind: "ok", value: result.value });
    setChange({ kind: "saved", text: done });
    return true;
  };

  const duplicate = (text: string, except: number | null) =>
    rules.some((rule, index) => index !== except && sameRule(rule, text));

  const saveEdit = async (event: FormEvent) => {
    event.preventDefault();
    if (editing === null) return;
    const text = editing.text.trim();
    if (text === "") return;
    if (duplicate(text, editing.index)) {
      setChange({ kind: "failed", text: "Esa regla ya está en la guía de estilo." });
      return;
    }
    const next = rules.map((rule, index) => (index === editing.index ? text : rule));
    if (await save(next, "Regla modificada.")) setEditing(null);
  };

  const remove = (index: number) => {
    setEditing(null);
    void save(
      rules.filter((_, at) => at !== index),
      `Regla borrada: «${rules[index]}».`,
    );
  };

  const add = async (event: FormEvent) => {
    event.preventDefault();
    const text = draft.trim();
    if (text === "") return;
    if (duplicate(text, null)) {
      setChange({ kind: "failed", text: "Esa regla ya está en la guía de estilo." });
      return;
    }
    if (await save([...rules, text], "Regla añadida.")) setDraft("");
  };

  const full = rules.length >= MAX_RULES;

  return (
    <main className="style-guide-page">
      <p className="crumbs">
        <a href="/">← Mesa de estudio</a>
      </p>
      <h1>Guía de estilo de {subjectName}</h1>
      <p className="style-guide-intro">
        Tus preferencias generales para esta asignatura. El editor las sigue en todos sus temas, al preparar y al revisar
        los apuntes.
      </p>
      {guide === null && <p>Cargando la guía de estilo…</p>}
      {guide !== null && guide.kind === "not-found" && <p role="alert">{guide.detail}</p>}
      {guide !== null && (guide.kind === "error" || guide.kind === "unreachable") && (
        <p role="alert">No se pudo cargar la guía de estilo: {describeFailure(guide)}</p>
      )}
      {guide?.kind === "ok" && (
        <>
          {rules.length === 0 ? (
            <p>
              Todavía no hay reglas. Puedes añadirlas aquí o guardar las que el editor te proponga en la conversación.
            </p>
          ) : (
            <ol className="style-guide-rules" aria-label="Reglas de la guía de estilo">
              {rules.map((rule, index) =>
                editing?.index === index ? (
                  <li key={`${index}-${rule}`}>
                    <form className="style-guide-edit" onSubmit={saveEdit}>
                      <label htmlFor="style-guide-edit-input">Regla {index + 1}</label>
                      <textarea
                        id="style-guide-edit-input"
                        rows={2}
                        maxLength={MAX_RULE_CHARS}
                        value={editing.text}
                        onChange={(event) => setEditing({ index, text: event.target.value })}
                      />
                      <div className="style-guide-actions">
                        <button type="submit" disabled={saving || editing.text.trim() === ""}>
                          Guardar
                        </button>
                        <button type="button" onClick={() => setEditing(null)} disabled={saving}>
                          Cancelar
                        </button>
                      </div>
                    </form>
                  </li>
                ) : (
                  <li key={`${index}-${rule}`}>
                    <span className="style-guide-rule">{rule}</span>
                    <span className="style-guide-actions">
                      <button
                        type="button"
                        aria-label={`Editar la regla: ${rule}`}
                        disabled={saving}
                        onClick={() => setEditing({ index, text: rule })}
                      >
                        Editar
                      </button>
                      <button
                        type="button"
                        aria-label={`Borrar la regla: ${rule}`}
                        disabled={saving}
                        onClick={() => remove(index)}
                      >
                        Borrar
                      </button>
                    </span>
                  </li>
                ),
              )}
            </ol>
          )}
          <form className="style-guide-add" onSubmit={add} aria-label="Añadir una regla">
            <label htmlFor="style-guide-new">Nueva regla</label>
            <textarea
              id="style-guide-new"
              rows={2}
              maxLength={MAX_RULE_CHARS}
              placeholder="Por ejemplo: «Usa tablas para comparar conceptos.»"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
            <button type="submit" disabled={saving || full || draft.trim() === ""}>
              Añadir
            </button>
            {full && <p>La guía ya tiene {MAX_RULES} reglas, el máximo: borra alguna para añadir otra.</p>}
          </form>
        </>
      )}
      <div aria-live="polite">
        {saving && <p>Guardando…</p>}
        {change?.kind === "saved" && <p>{change.text}</p>}
      </div>
      {change?.kind === "failed" && <p role="alert">{change.text}</p>}
    </main>
  );
}
