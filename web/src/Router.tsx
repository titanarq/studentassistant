import App from "./App";
import CapturePage from "./capture/CapturePage";
import PairPage from "./pairing/PairPage";
import TopicPage from "./topic/TopicPage";

const TOPIC_PATH = /^\/subjects\/([^/]+)\/topics\/([^/]+)$/;

function decode(segment: string): string | null {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
}

/**
 * Picks the page for the current path. The backend serves the built app for every non-API
 * path (SPA fallback, #85), so a plain `pathname` switch is all the routing the app needs.
 */
export default function Router({ pathname = window.location.pathname }: { pathname?: string }) {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/pair") return <PairPage />;
  // The capture client (#40): the student picks the subject and topic, opens the session and the
  // page gives way to the capture screen that runs it.
  if (path === "/capture") return <CapturePage />;
  const topic = TOPIC_PATH.exec(path);
  if (topic) {
    const subjectId = decode(topic[1]);
    const topicId = decode(topic[2]);
    if (subjectId !== null && topicId !== null) {
      return <TopicPage subjectId={subjectId} topicId={topicId} />;
    }
  }
  return <App />;
}
