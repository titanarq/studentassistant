import { type FeedbackRef, feedbackLabel } from "./feedback";
import "./feedbackChip.css";

/** The chip of a chat turn that recorded app feedback (#472); its title as the tooltip. */
export default function FeedbackChip({ feedback }: { feedback: FeedbackRef }) {
  const label = feedbackLabel(feedback);
  return (
    <p className="feedback-chip-row">
      <span
        className={`feedback-chip feedback-chip-${feedback.kind}`}
        title={feedback.title !== "" ? feedback.title : undefined}
        data-testid="feedback-chip"
      >
        {label}
        {feedback.title !== "" && <span className="feedback-chip-sr">: {feedback.title}</span>}
      </span>
    </p>
  );
}
