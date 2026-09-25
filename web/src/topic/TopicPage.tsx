import { useCallback, useEffect, useState } from "react";
import {
  describeFailure,
  fetchSubjects,
  fetchTopics,
  fetchTopicSummary,
  type ReadResult,
  type TopicSummary,
} from "../desk/api";
import MaterialsPanel from "../materials/MaterialsPanel";
import PdfUploadForm from "./PdfUploadForm";
import PrepareTopic from "./PrepareTopic";
import TopicCard from "./TopicCard";
import WebPageForm from "./WebPageForm";
import WebSearchPanel from "./WebSearchPanel";

/**
 * `/subjects/<subject>/topics/<topic>`: the topic page, reached from the study desk. It shows
 * "<subject> / <topic>" (names from the subject and topic lists, ids until they arrive), the
 * topic card from the read API's summary, "Material de estudio" (`MaterialsPanel`, #79: generate,
 * preview and download each material; a generation reloads the card), and the PDF upload (#149), after which the card is
 * reloaded so the new source is counted, and "Prepárame el tema" (`PrepareTopic`), after which
 * the card is reloaded too, "Buscar en Internet" (`WebSearchPanel`, #59), after keeping a page, and
 * "Añadir una página web" (`WebPageForm`, #62), after storing one.
 */
export default function TopicPage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [subjectName, setSubjectName] = useState(subjectId);
  const [topicName, setTopicName] = useState(topicId);
  const [summary, setSummary] = useState<ReadResult<TopicSummary> | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    fetchSubjects().then((result) => {
      const subject = result.kind === "ok" ? result.value.find((s) => s.subject_id === subjectId) : undefined;
      if (!cancelled && subject) setSubjectName(subject.name);
    });
    fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (!cancelled && topic) setTopicName(topic.name);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  useEffect(() => {
    let cancelled = false;
    fetchTopicSummary(subjectId, topicId).then((result) => {
      if (!cancelled) setSummary(result);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, reload]);

  const refresh = useCallback(() => setReload((n) => n + 1), []);

  return (
    <main>
      <p>
        <a href="/">← Mesa de estudio</a>
      </p>
      <h1>Tema {topicName}</h1>
      <p>Asignatura {subjectName}</p>
      {summary === null && <p>Cargando el tema…</p>}
      {summary !== null && summary.kind === "ok" && <TopicCard summary={summary.value} />}
      {summary !== null && summary.kind !== "ok" && (
        <p role="alert">No se pudo cargar el resumen del tema: {describeFailure(summary)}</p>
      )}
      {(summary === null || summary.kind !== "not-found") && (
        <>
          <PrepareTopic subjectId={subjectId} topicId={topicId} onDone={refresh} />
          <MaterialsPanel subjectId={subjectId} topicId={topicId} refreshKey={reload} onGenerated={refresh} />
          <PdfUploadForm subjectId={subjectId} topicId={topicId} onImported={refresh} />
          <WebSearchPanel subjectId={subjectId} topicId={topicId} onKept={refresh} />
          <WebPageForm subjectId={subjectId} topicId={topicId} onAdded={refresh} />
        </>
      )}
    </main>
  );
}
