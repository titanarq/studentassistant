/**
 * The `/capture` page (#40): the two steps of a capture client on the laptop. First the picker,
 * which chooses the subject and the topic and opens the session; then, for as long as that session
 * runs, the capture screen, which is the camera, the buttons and the live transcript. Nothing of
 * the session survives the page: no token, no spool and no state in the browser, because the
 * backend is on this same PC and trusts loopback (docs/modules/server.md).
 *
 * A session belongs to exactly one topic and there is no topic switch inside it, so the screen
 * takes the session it is given and the way back to another topic is to end it: `onEnded` returns
 * the page to the picker. The screen is keyed by the session id, which makes a second session in
 * the same page visit a new component rather than the old one with new props.
 *
 * The picker also leads to the voice tutor (#82) of the chosen topic, which only reads the topic
 * and opens no session; "Volver" returns to the picker.
 *
 * With `preset` (the study workspace's **Captura** tab, #312) the subject and topic are given:
 * `TopicSessionStart` replaces the picker (no tutor), and `onRunningChange` tells the host
 * whether a session is on screen.
 */

import { useCallback, useEffect, useState } from "react";
import CaptureScreen from "./CaptureScreen";
import TutorScreen from "../tutor/TutorScreen";
import SessionPicker, { type OpenedSession, type TutorTopic } from "./SessionPicker";
import TopicSessionStart from "./TopicSessionStart";

export interface CapturePageProps {
  /** The client clock every `client_time_ms` this page sends is read from. */
  now?: () => number;
  /** The subject and topic to capture, chosen by the host page: no picker is shown. */
  preset?: { subjectId: string; topicId: string };
  /** Called with true when a session's screen takes over and false when it gives way. */
  onRunningChange?: (running: boolean) => void;
}

export default function CapturePage({ now = Date.now, preset, onRunningChange }: CapturePageProps) {
  const [opened, setOpened] = useState<OpenedSession | null>(null);
  const running = opened !== null;
  useEffect(() => {
    onRunningChange?.(running);
  }, [running, onRunningChange]);

  const onSession = useCallback((session: OpenedSession) => setOpened(session), []);
  const onEnded = useCallback(() => setOpened(null), []);
  const [tutor, setTutor] = useState<TutorTopic | null>(null);
  const onTutor = useCallback((topic: TutorTopic) => setTutor(topic), []);
  const onTutorClosed = useCallback(() => setTutor(null), []);

  if (opened === null && preset !== undefined) {
    return <TopicSessionStart subjectId={preset.subjectId} topicId={preset.topicId} onSession={onSession} now={now} />;
  }
  if (opened === null && tutor !== null) {
    return <TutorScreen key={`${tutor.subjectId}/${tutor.topicId}`} {...tutor} onClose={onTutorClosed} />;
  }
  if (opened === null) return <SessionPicker onSession={onSession} now={now} onTutor={onTutor} />;
  return (
    <CaptureScreen
      key={opened.session.session_id}
      session={opened.session}
      subjectName={opened.subjectName}
      topicName={opened.topicName}
      now={now}
      onEnded={onEnded}
    />
  );
}
