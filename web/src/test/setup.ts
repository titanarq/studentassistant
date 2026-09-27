import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";
import { LOAD_TIMEOUT } from "./timeouts";

// Every findBy*/waitFor waits up to LOAD_TIMEOUT, not the default 1 s, so a loaded host does not
// fail a wait for something that is on its way (#430).
configure({ asyncUtilTimeout: LOAD_TIMEOUT });
