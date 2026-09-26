import { useEffect, useMemo, useState } from "react";
import { describeFailure, fetchTopics, type ReadResult, topicPath } from "../desk/api";
import { parseNotes } from "../notes/markdown";
import NotesView from "../notes/NotesView";
import { type Artifact, fetchGeneratedText, fetchMaterials, fileUrl } from "./api";
import "../notes/notes.css";
import "./materials.css";

/**
 * `/subjects/<subject>/topics/<topic>/material/<name>` (#79): the preview of a generated Markdown
 * file (`esquema.md`, `examen.md`, `examen-soluciones.md`, `diapositivas.md`), read through
 * `GET .../generated/files/<name>` and rendered by the notes' Markdown reader (`NotesView`), so no
 * HTML of the file reaches the DOM as markup; a mind map or a Marp deck shows as its source. It
 * says which notes version it was built from, notes when it is stale (from `GET .../generated`),
 * and links the `.md` for download.
 */

const noSource = () => undefined;

export default function MaterialPreviewPage({
  subjectId,
  topicId,
  name,
}: {
  subjectId: string;
  topicId: string;
  name: string;
}) {
  const [topicName, setTopicName] = useState(topicId);
  const [text, setText] = useState<ReadResult<string> | null>(null);
  const [artifact, setArtifact] = useState<Artifact | null>(null);

  useEffect(() => {
    let cancelled = false;
    setText(null);
    setArtifact(null);
    fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (!cancelled && topic) setTopicName(topic.name);
    });
    fetchGeneratedText(subjectId, topicId, name).then((result) => {
      if (!cancelled) setText(result);
    });
    fetchMaterials(subjectId, topicId).then((result) => {
      if (cancelled || result.kind !== "ok") return;
      setArtifact(result.value.artifacts.find((a) => a.files.includes(name)) ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, name]);

  const tree = useMemo(() => (text?.kind === "ok" ? parseNotes(text.value) : null), [text]);
  const stem = name.replace(/\.md$/i, "");
  // "Ejercicios y examen (examen-soluciones)": a kind with several Markdown files names the file too.
  const title = artifact?.title == null ? stem : stem === artifact.kind ? artifact.title : `${artifact.title} (${stem})`;

  return (
    <main className="material-preview">
      <p className="crumbs">
        <a href={topicPath(subjectId, topicId)}>← Tema {topicName}</a>
        <a className="crumbs-home" href="/">
          Mesa de estudio
        </a>
      </p>
      <h1>
        {title} de {topicName}
      </h1>
      <p className="material-description">
        {artifact?.notesVersion != null && `De los apuntes v${artifact.notesVersion} · `}
        <a href={fileUrl(subjectId, topicId, name)} download>
          Descargar {name}
        </a>
      </p>
      {artifact?.stale && (
        <p role="note" className="material-stale-reason">
          <span className="material-stale">Desactualizado</span>{" "}
          {artifact.staleReason ?? "Los apuntes han cambiado desde que se generó."}
        </p>
      )}
      {text === null && <p>Cargando el material…</p>}
      {text !== null && text.kind !== "ok" && (
        <p role="alert">No se pudo cargar el material: {describeFailure(text)}</p>
      )}
      {tree !== null && <NotesView tree={tree} onOpenSource={noSource} />}
    </main>
  );
}
