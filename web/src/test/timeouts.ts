/**
 * Bounds for the waits of the web tests (#430). Under host load (every core busy) Testing
 * Library's role queries cost hundreds of milliseconds each, since they compute accessible names
 * over jsdom, so the first render of a page and each poll of a `findBy*` can outlast the default
 * 1 s wait, and a test that drives a page through several reads can outlast vitest's 5 s.
 */

/** Bound for a `findBy*` / `waitFor` on something a page renders from its reads; the default of every async util (setup.ts). */
export const LOAD_TIMEOUT = 5000;

/**
 * Per-test bound (the third argument of `it`) for a test that drives a page through several
 * reads, above vitest's default 5 s. Never raise the global `testTimeout` instead.
 */
export const PAGE_TEST_TIMEOUT = 15000;
