import App from "./App";
import PairPage from "./pairing/PairPage";

/**
 * Picks the page for the current path. The backend serves the built app for every non-API
 * path (SPA fallback, #85), so a plain `pathname` switch is all the routing the app needs.
 */
export default function Router({ pathname = window.location.pathname }: { pathname?: string }) {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/pair") return <PairPage />;
  return <App />;
}
