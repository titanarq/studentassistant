import { useEffect, useState } from "react";

const DARK = "(prefers-color-scheme: dark)";

function prefersDark(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(DARK).matches;
}

/**
 * Whether the app is showing its dark theme. The app has no theme switch of its own: the dark
 * theme is the OS setting (`prefers-color-scheme`, styles/tokens.css), so this follows that media
 * query and updates when it changes.
 */
export function useDarkScheme(): boolean {
  const [dark, setDark] = useState(prefersDark);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(DARK);
    const update = () => setDark(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return dark;
}
