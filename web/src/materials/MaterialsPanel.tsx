import { useCallback, useEffect, useRef, useState } from "react";
import { describeFailure, type ReadResult, topicPath } from "../desk/api";
import { type ActionResult, describeActionFailure } from "../pending/doubts";
import {
  type Artifact,
  fetchGenerators,
  fetchMaterials,
  fileUrl,
  type Generated,
  generateMaterial,
  type Materials,
  previewPath,
} from "./api";
import "./materials.css";

/**
 * "Material de estudio" on the topic page (#79): every study material of the topic -- the kinds
 * `GET .../generated` reports, in the order of study (esquema, quiz, flashcards, examen,
 * diapositivas, then any other) -- with whether it was generated, from which notes version, and a
 * "Desactualizado" badge with the backend's reason when the notes changed since. Each registered
 * kind has "Generar" ("Generar de nuevo" once generated), with default options, that posts
 * `POST .../generated/<kind>`; past the cost cap it offers "Generar igualmente". Its Markdown files
 * link to the preview page ("Ver ..."), the quiz to its page, and the Anki deck, CSV, PDF and
 * PowerPoint files are download links. After a generation the section is read again and
 * `onGenerated` lets the page reload the topic card.
 */

/** The order of study; kinds not listed follow, alphabetically. */
export const STUDY_ORDER = ["esquema", "quiz", "flashcards", "examen", "diapositivas"];

/** Downloadable files by extension, with the label shown. */
const DOWNLOADS: Record<string, string> = { apkg: "Anki", csv: "CSV", pdf: "PDF", pptx: "PowerPoint" };

/** Extra pages for a kind, beside its files. */
const PAGES: Record<string, { path: string; label: string }> = { quiz: { path: "quiz", label: "Hacer el quiz" } };

function extension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function stem(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

export function ordered(artifacts: Artifact[]): Artifact[] {
  const rank = (kind: string) => {
    const at = STUDY_ORDER.indexOf(kind);
    return at < 0 ? STUDY_ORDER.length : at;
  };
  return [...artifacts].sort((a, b) => rank(a.kind) - rank(b.kind) || a.kind.localeCompare(b.kind));
}

function formatBuiltAt(iso: string): string | null {
  const time = Date.parse(iso);
  return Number.isNaN(time)
    ? null
    : new Date(time).toLocaleString("es-ES", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
}

function stateText(artifact: Artifact): string {
  if (!artifact.generated) return "○ Sin generar";
  const parts = ["✓ Generado"];
  const when = artifact.builtAt === null ? null : formatBuiltAt(artifact.builtAt);
  if (when !== null) parts[0] += ` el ${when}`;
  if (artifact.notesVersion !== null) parts.push(`de los apuntes v${artifact.notesVersion}`);
  return parts.join(" · ");
}

type Failure = Exclude<ActionResult<Generated>, { kind: "ok" }>;

function MaterialItem({
  subjectId,
  topicId,
  artifact,
  description,
  hasNotes,
  onGenerated,
}: {
  subjectId: string;
  topicId: string;
  artifact: Artifact;
  description: string;
  hasNotes: boolean;
  onGenerated: () => void;
}) {
  const [generating, setGenerating] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const title = artifact.title ?? artifact.kind;
  const registered = artifact.title !== null;

  async function generate(confirmOverCap: boolean) {
    setGenerating(true);
    setFailure(null);
    setWarnings([]);
    const result = await generateMaterial(subjectId, topicId, artifact.kind, confirmOverCap);
    if (!mounted.current) return;
    setGenerating(false);
    if (result.kind === "ok") {
      setWarnings(result.value.warnings);
      onGenerated();
    } else {
      setFailure(result);
    }
  }

  const previews = artifact.files.filter((name) => extension(name) === "md" && !name.includes("/"));
  const downloads = artifact.files.filter((name) => DOWNLOADS[extension(name)] !== undefined);
  const page = PAGES[artifact.kind];

  return (
    <li className="material" aria-label={title}>
      <h3>{title}</h3>
      {description !== "" && <p className="material-description">{description}</p>}
      <p>
        {stateText(artifact)}
        {artifact.generated && artifact.stale && (
          <>
            {" "}
            <span className="material-stale">Desactualizado</span>
          </>
        )}
      </p>
      {artifact.generated && artifact.stale && artifact.staleReason !== null && (
        <p className="material-stale-reason">{artifact.staleReason}</p>
      )}
      {(previews.length > 0 || downloads.length > 0 || (artifact.generated && page !== undefined)) && (
        <p className="material-links">
          {artifact.generated && page !== undefined && (
            <a href={`${topicPath(subjectId, topicId)}/${page.path}`}>{page.label}</a>
          )}
          {previews.map((name) => (
            <a key={name} href={previewPath(subjectId, topicId, name)}>
              Ver {stem(name)}
            </a>
          ))}
          {downloads.map((name) => (
            <a key={name} href={fileUrl(subjectId, topicId, name)} download>
              {`${stem(name)} (${DOWNLOADS[extension(name)]})`}
            </a>
          ))}
        </p>
      )}
      {registered && (
        <p>
          <button type="button" disabled={generating || !hasNotes} onClick={() => void generate(false)}>
            {generating ? "Generando…" : artifact.generated ? "Generar de nuevo" : "Generar"}
          </button>
        </p>
      )}
      {failure !== null && (
        <p role="alert">
          {describeActionFailure(failure)}{" "}
          {failure.kind === "refused" && failure.overCap && (
            <button type="button" disabled={generating} onClick={() => void generate(true)}>
              Generar igualmente
            </button>
          )}
        </p>
      )}
      {warnings.length > 0 && (
        <ul className="material-warnings" aria-label={`Avisos de ${title}`}>
          {warnings.map((warning, index) => (
            <li key={index}>{warning}</li>
          ))}
        </ul>
      )}
    </li>
  );
}

export default function MaterialsPanel({
  subjectId,
  topicId,
  refreshKey = 0,
  onGenerated,
}: {
  subjectId: string;
  topicId: string;
  /** Read the materials again when it changes (the page's reload after "Prepárame el tema"...). */
  refreshKey?: number;
  /** A generation finished; without it the section reads itself again, with it the caller does. */
  onGenerated?: () => void;
}) {
  const [materials, setMaterials] = useState<ReadResult<Materials> | null>(null);
  const [descriptions, setDescriptions] = useState<Record<string, string>>({});
  const reads = useRef(0);

  // Only the latest read is shown, so an older answer never overwrites a newer state.
  const load = useCallback(async () => {
    const read = ++reads.current;
    const result = await fetchMaterials(subjectId, topicId);
    if (read === reads.current) setMaterials(result);
  }, [subjectId, topicId]);

  useEffect(() => {
    void load();
    return () => {
      reads.current++;
    };
  }, [load, refreshKey]);

  useEffect(() => {
    let cancelled = false;
    fetchGenerators().then((result) => {
      if (cancelled || result.kind !== "ok") return;
      setDescriptions(Object.fromEntries(result.value.map((info) => [info.kind, info.description])));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const generated = useCallback(() => {
    if (onGenerated) onGenerated();
    else void load();
  }, [load, onGenerated]);

  return (
    <section className="materials" aria-labelledby="materials-heading">
      <h2 id="materials-heading">Material de estudio</h2>
      {materials === null && <p>Cargando el material…</p>}
      {materials !== null && materials.kind !== "ok" && (
        <p>No se pudo cargar el material del tema: {describeFailure(materials)}</p>
      )}
      {materials !== null && materials.kind === "ok" && (
        <>
          {!materials.value.hasNotes && (
            <p>Todavía no hay apuntes: prepara el tema para poder generar el material.</p>
          )}
          <ul className="materials-list">
            {ordered(materials.value.artifacts).map((artifact) => (
              <MaterialItem
                key={artifact.kind}
                subjectId={subjectId}
                topicId={topicId}
                artifact={artifact}
                description={descriptions[artifact.kind] ?? ""}
                hasNotes={materials.value.hasNotes}
                onGenerated={generated}
              />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
