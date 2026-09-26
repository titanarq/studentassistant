// Machine-readable codes of REST error bodies (protocol 1.2, protocol/README.md "REST errors").
// A REST error body is `{"detail": "<Spanish sentence>", "code"?: "<code>"}`; clients branch on
// `code`, never on the wording of `detail`. Error bodies are read leniently: an unknown or
// missing code is `null`, which every caller treats as "no code".

export const ERROR_CODES = ["cost_cap_reached", "doubt_closed", "session_open", "notes_changed", "notes_busy"] as const;

/**
 * - `cost_cap_reached` (409): the session's or the day's cost cap is reached; retrying with
 *   `confirm_over_cap` goes past it.
 * - `doubt_closed` (409): the doubt was already answered, auto-resolved or dismissed.
 * - `session_open` (409): an unended session is in the way.
 * - `notes_changed` (409): a student save (`PUT .../notes`) named a stale `base_revision`; the body
 *   also carries the current notes' `text` and `revision`.
 * - `notes_busy` (409): "prepárame el tema", a restore or another rewrite holds the topic's notes.
 */
export type ErrorCode = (typeof ERROR_CODES)[number];

export function isErrorCode(value: unknown): value is ErrorCode {
  return typeof value === "string" && (ERROR_CODES as readonly string[]).includes(value);
}

/** The `code` of an error body, or `null` when it has none this side knows. */
export function errorCode(body: unknown): ErrorCode | null {
  if (typeof body !== "object" || body === null) return null;
  const code = (body as { code?: unknown }).code;
  return isErrorCode(code) ? code : null;
}
