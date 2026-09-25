// Locates the repository's own `protocol/examples/` directory (never a copy inside `web/`) and
// reads its shared example messages for the contract tests.

import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const FROM_THIS_MODULE = new URL("../../../protocol/examples/", import.meta.url);

/**
 * `web/src/test/` -> repository root -> `protocol/examples/`, with the separator `sharedExamples`
 * concatenates onto. A test of the DOM environment gets an http `import.meta.url` in the modules
 * it imports, so there the vitest root (`web/`) locates the same directory.
 */
export const EXAMPLES_DIR =
  FROM_THIS_MODULE.protocol === "file:"
    ? fileURLToPath(FROM_THIS_MODULE)
    : `${resolve(process.cwd(), "../protocol/examples")}/`;

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
