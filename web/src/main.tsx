import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// The design system first, so page CSS (imported by the pages) builds on it.
import "./styles/index.css";
import Router from "./Router";
import { ConfirmProvider } from "./ui/ConfirmDialog";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root element");

createRoot(root).render(
  <StrictMode>
    {/* The one confirmation modal of the app (#486): every page asks through `useConfirm()`. */}
    <ConfirmProvider>
      <Router />
    </ConfirmProvider>
  </StrictMode>,
);
