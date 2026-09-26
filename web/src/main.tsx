import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// The design system first, so page CSS (imported by the pages) builds on it.
import "./styles/index.css";
import Router from "./Router";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root element");

createRoot(root).render(
  <StrictMode>
    <Router />
  </StrictMode>,
);
