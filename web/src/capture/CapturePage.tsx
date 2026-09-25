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
 */

import { useCallback, useState } from "react";
import CaptureScreen from "./CaptureScreen";
import SessionPicker, { type OpenedSession } from "./SessionPicker";

export interface CapturePageProps {
  /** The client clock every `client_time_ms` this page sends is read from. */
  now?: () => number;
}

export default function CapturePage({ now = Date.now }: CapturePageProps) {
  const [opened, setOpened] = useState<OpenedSession | null>(null);

  const onSession = useCallback((session: OpenedSession) => setOpened(session), []);
  const onEnded = useCallback(() => setOpened(null), []);

  if (opened === null) return <SessionPicker onSession={onSession} now={now} />;
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
