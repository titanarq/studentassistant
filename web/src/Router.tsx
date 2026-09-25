import App from "./App";
import LivePage from "./live/LivePage";
import PairPage from "./pairing/PairPage";
import NotesPage from "./notes/NotesPage";
import PendingPage from "./pending/PendingPage";
import StyleGuidePage from "./styleGuide/StyleGuidePage";
import TopicPage from "./topic/TopicPage";
import VersionsPage from "./versions/VersionsPage";

const STYLE_GUIDE_PATH = /^\/subjects\/([^/]+)\/style-guide$/;
const TOPIC_PATH = /^\/subjects\/([^/]+)\/topics\/([^/]+)(\/notes|\/pending|\/versions)?$/;

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
  if (path === "/live") return <LivePage />;
  const guide = STYLE_GUIDE_PATH.exec(path);
  if (guide) {
    const subjectId = decode(guide[1]);
    if (subjectId !== null) return <StyleGuidePage subjectId={subjectId} />;
  }
  const topic = TOPIC_PATH.exec(path);
  if (topic) {
    const subjectId = decode(topic[1]);
    const topicId = decode(topic[2]);
    if (subjectId !== null && topicId !== null) {
      if (topic[3] === "/notes") return <NotesPage subjectId={subjectId} topicId={topicId} />;
      if (topic[3] === "/versions") return <VersionsPage subjectId={subjectId} topicId={topicId} />;
      if (topic[3] === "/pending") return <PendingPage subjectId={subjectId} topicId={topicId} />;
      return <TopicPage subjectId={subjectId} topicId={topicId} />;
    }
  }
  return <App />;
}
