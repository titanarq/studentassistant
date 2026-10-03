// Machine-readable codes of REST error bodies (protocol 1.2, protocol/README.md "REST errors").
// A REST error body is `{"detail": "<Spanish sentence>", "code"?: "<code>"}`; clients branch on
// `code`, never on the wording of `detail`. Error bodies are read leniently: an unknown or
// missing code is `null`, which every caller treats as "no code".

export const ERROR_CODES = [
  "cost_cap_reached",
  "doubt_closed",
  "session_open",
  "notes_changed",
  "notes_busy",
  "user_required",
  "user_not_found",
] as const;

/** The protocol version that added `code` to REST error bodies, and the version of every code below
 * except the two user ones. */
export const ERROR_CODE_SINCE = [1, 2] as const;

/** The protocol version that added `user_required` and `user_not_found` (#545). */
export const USER_ERROR_CODES_SINCE = [1, 8] as const;

/**
 * - `cost_cap_reached` (409): the session's or the day's cost cap is reached; retrying with
 *   `confirm_over_cap` goes past it.
 * - `doubt_closed` (409): the doubt was already answered, auto-resolved or dismissed.
 * - `session_open` (409): an unended session is in the way.
 * - `notes_changed` (409): a student save (`PUT .../notes`) named a stale `base_revision`; the body
 *   also carries the current notes' `text` and `revision`.
 * - `notes_busy` (409): "prepárame el tema", a restore or another rewrite holds the topic's notes.
 * - `user_required` (400): the request does not say which user it acts for (`X-SA-User` / `sa_user`)
 *   and the vault holds more than one, so none can be assumed.
 * - `user_not_found` (404): this vault has no user with the id the request named.
 */
export type ErrorCode = (typeof ERROR_CODES)[number];

/**
 * The protocol version each code was added in. Codes are additive: the backend sends one only to a
 * client that negotiated at least that version, so a client of an older one never sees the two user
 * codes -- and treats any code it does not know as none, as `errorCode` already does.
 */
export const ERROR_CODES_SINCE: Record<ErrorCode, readonly [number, number]> = {
  cost_cap_reached: ERROR_CODE_SINCE,
  doubt_closed: ERROR_CODE_SINCE,
  session_open: ERROR_CODE_SINCE,
  notes_changed: ERROR_CODE_SINCE,
  notes_busy: ERROR_CODE_SINCE,
  user_required: USER_ERROR_CODES_SINCE,
  user_not_found: USER_ERROR_CODES_SINCE,
};

export function isErrorCode(value: unknown): value is ErrorCode {
  return typeof value === "string" && (ERROR_CODES as readonly string[]).includes(value);
}

/** The `code` of an error body, or `null` when it has none this side knows. */
export function errorCode(body: unknown): ErrorCode | null {
  if (typeof body !== "object" || body === null) return null;
  const code = (body as { code?: unknown }).code;
  return isErrorCode(code) ? code : null;
}
