import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";
import { installDialogPolyfill } from "./dialog";
import { LOAD_TIMEOUT } from "./timeouts";
import { vi } from "vitest";

// Every findBy*/waitFor waits up to LOAD_TIMEOUT, not the default 1 s, so a loaded host does not
// fail a wait for something that is on its way (#430).
configure({ asyncUtilTimeout: LOAD_TIMEOUT });

// jsdom has no dialog behaviour (#486): `showModal()`, `close()` and Escape's `cancel`; see dialog.ts.
installDialogPolyfill();

// No test loads the real mermaid (#479); see fakeMermaid.ts.
vi.mock("mermaid", async () => {
  const { fakeMermaid } = await import("./fakeMermaid");
  fakeMermaid.loads++;
  return { default: fakeMermaid };
});
