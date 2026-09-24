// Locates the repository's own `protocol/examples/` directory (never a copy inside `web/`) and
// reads its shared example messages for the contract tests.

import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** `web/src/test/` -> repository root -> `protocol/examples/`. */
export const EXAMPLES_DIR = fileURLToPath(new URL("../../../protocol/examples/", import.meta.url));

export interface SharedExample {
  /** Schema name, e.g. `client.hello` for `client.hello.json`. */
  name: string;
  json: unknown;
}

export function sharedExamples(): SharedExample[] {
  return readdirSync(EXAMPLES_DIR)
    .filter((file) => file.endsWith(".json"))
    .sort()
    .map((file) => ({
      name: file.slice(0, -".json".length),
      json: JSON.parse(readFileSync(EXAMPLES_DIR + file, "utf8")) as unknown,
    }));
}
