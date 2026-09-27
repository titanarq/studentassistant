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
 * `TopicSessionStart` replaces the picker (no tutor), `onRunningChange` tells the host whether a
 * session is on screen, and the screen runs `embedded` (#413). An end there goes straight back to
 * `TopicSessionStart`: the student is already in Construir.
 *
 * Standalone, **Terminar** leads to `SessionEnded` (#413): the topic's notes are built in its
 * workspace, so the step links to it («Abrir en Construir») and offers the picker again. The web
 * has no «Terminar y preparar apuntes» (human decision 2026-09-27, it stays on the Android app):
 * the whole topic is prepared by asking the workspace chat («prepárame el tema»).
 */

import { useCallback, useEffect, useState } from "react";
import { topicPath } from "../desk/api";
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
  /**
   * Since #450: the host hides the page (the workspace shows **Recursos**), so a running capture
   * pauses its camera and microphone until it turns false again (`CaptureScreen`'s `suspended`).
   */
  suspended?: boolean;
}

export default function CapturePage({ now = Date.now, preset, onRunningChange, suspended = false }: CapturePageProps) {
  const [opened, setOpened] = useState<OpenedSession | null>(null);
  const running = opened !== null;
  useEffect(() => {
    onRunningChange?.(running);
  }, [running, onRunningChange]);

  const [ended, setEnded] = useState<OpenedSession | null>(null);
  const standalone = preset === undefined;
  const onSession = useCallback((session: OpenedSession) => setOpened(session), []);
  const onEnded = useCallback(() => {
    if (standalone) setEnded(opened);
    setOpened(null);
  }, [standalone, opened]);
  const onEndedClosed = useCallback(() => setEnded(null), []);
  const [tutor, setTutor] = useState<TutorTopic | null>(null);
  const onTutor = useCallback((topic: TutorTopic) => setTutor(topic), []);
  const onTutorClosed = useCallback(() => setTutor(null), []);

  if (opened === null && preset !== undefined) {
    return <TopicSessionStart subjectId={preset.subjectId} topicId={preset.topicId} onSession={onSession} now={now} />;
  }
  if (opened === null && ended !== null) {
    return <SessionEnded ended={ended} onClose={onEndedClosed} />;
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
      embedded={!standalone}
      suspended={suspended}
    />
  );
}

/**
 * The standalone page's step after **Terminar** (#413): the session is over, and the topic goes
 * on in its workspace, whose chat prepares the whole topic on request.
 */
function SessionEnded({ ended, onClose }: { ended: OpenedSession; onClose: () => void }) {
  const { subject_id: subjectId, topic_id: topicId } = ended.session;
  return (
    <main className="capture-page capture-ended">
      <header className="capture-header">
        <h1>Sesión terminada</h1>
        <p className="page-context">
          {ended.subjectName} · {ended.topicName}
        </p>
      </header>
      <section className="capture-step" aria-label="Qué hacer ahora">
        <h2>Y ahora</h2>
        <p>
          Lo que has capturado ya está guardado. Los apuntes del tema se construyen en Construir:
          allí puedes pedirle al chat «prepárame el tema».
        </p>
        <p>
          <a href={`${topicPath(subjectId, topicId)}/workspace`}>Abrir en Construir</a>
        </p>
        <button type="button" onClick={onClose}>
          Volver a la lista de sesiones
        </button>
      </section>
    </main>
  );
}
