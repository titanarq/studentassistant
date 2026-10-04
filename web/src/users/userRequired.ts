import { errorCode } from "../protocol/errors";

/** Fired on `window` when the backend says the active user is missing or unknown. */
export const USER_REQUIRED_EVENT = "sa:user-required";

/**
 * Pages call this where they already map an error body: a `400 user_required` or
 * `404 user_not_found` answer (`errorCode(body)`) means the chosen user is no longer valid, so the
 * `UserGate` shows the selection again. Returns true when the body was one of those two answers.
 */
export function reportUserError(body: unknown): boolean {
  const code = errorCode(body);
  if (code !== "user_required" && code !== "user_not_found") return false;
  window.dispatchEvent(new Event(USER_REQUIRED_EVENT));
  return true;
}
