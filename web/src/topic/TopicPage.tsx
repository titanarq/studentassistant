import PdfUploadForm from "./PdfUploadForm";

/**
 * `/subjects/<subject>/topics/<topic>`: the topic page. For now it only hosts the PDF upload
 * (#149); the topic card, notes and sources come with the study desk.
 */
export default function TopicPage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  return (
    <main>
      <h1>Tema {topicId}</h1>
      <p>Asignatura {subjectId}</p>
      <PdfUploadForm subjectId={subjectId} topicId={topicId} />
    </main>
  );
}
