/**
 * The question chat of the study screen (#333): a component boundary only. #336 replaces it
 * with typed questions about the document, answered citing its sections and sources.
 */
export const CHAT_PLACEHOLDER = "Aquí podrás preguntar sobre el documento.";

export default function StudyChatSlot() {
  return (
    <section className="study-chat panel" aria-label="Preguntas sobre el documento">
      <h2>Preguntas</h2>
      <p className="study-chat-placeholder">{CHAT_PLACEHOLDER}</p>
    </section>
  );
}
