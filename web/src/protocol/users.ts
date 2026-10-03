// The users of a shared vault and the active user of a request (protocol 1.8, #544).
//
// Several students share one backend and one vault, each with their own `users/<id>/` folder. The
// bodies below are what `GET /api/users`, `POST /api/users` and `PATCH /api/users/{user_id}`
// exchange; the profile photo has no JSON body (`PUT /api/users/{user_id}/photo` takes the raw
// image of one of `USER_PHOTO_CONTENT_TYPES` and answers with the updated `User`).
//
// Which user any other route acts for comes from the request itself: the `USER_HEADER` header, else
// the `USER_COOKIE` cookie (a present header wins), else -- when the vault holds exactly one user --
// that user, else a `400 user_required` refusal; an unknown id is `404 user_not_found`. The
// WebSocket handshake follows the same rule. This selects a user, it does not authenticate one: the
// bearer/loopback trust of ADR-0001 is unchanged and a device pairs with the backend, not with a
// user.

import { array, type Decoder, id, object, refine, str } from "./decode";

/** Request header naming the user a request acts for (the native Android app). */
export const USER_HEADER = "X-SA-User";

/** Cookie naming the same user (this page and the Android WebView). */
export const USER_COOKIE = "sa_user";

/** The longest `User.name`. */
export const USER_NAME_MAX_CHARS = 80;

/** The longest `User.email` (RFC 5321's limit for an address). */
export const USER_EMAIL_MAX_CHARS = 254;

/** The image types `PUT /api/users/{user_id}/photo` accepts; `GET` answers `image/jpeg`. */
export const USER_PHOTO_CONTENT_TYPES = ["image/jpeg", "image/png", "image/webp"] as const;

export type UserPhotoContentType = (typeof USER_PHOTO_CONTENT_TYPES)[number];

// A name travels trimmed: no whitespace at either end and at least one character that is not
// whitespace, so a blank name is impossible. A form taking a name from a text field trims it before
// sending it (#553).
export const USER_NAME_PATTERN = /^\S(?:.*\S)?$/;
export const USER_EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
export const USER_PHOTO_URL_PATTERN = /^\/api\/users\//;

/**
 * A student of this vault: the body of the create and update responses, and an item of the list
 * response. `email` and `photo_url` are absent, never `null`, when the user has none.
 */
export interface User {
  /** The slug of the name given at creation, with a numeric suffix when that slug is taken; a
   * later rename never changes it. */
  id: string;
  /** The student's name as they write it, trimmed: 1 to `USER_NAME_MAX_CHARS` characters, not blank. */
  name: string;
  /** Contact email; absent when the user has none. */
  email?: string;
  /** Path of the profile photo, served by `GET /api/users/{user_id}/photo`; absent when there is none. */
  photo_url?: string;
}

/** `GET /api/users`: every user of this vault. */
export interface UsersListResponse {
  users: User[];
}

/** `POST /api/users`: the name of the new user and, optionally, their email. */
export interface UserCreateRequest {
  name: string;
  email?: string;
}

/**
 * `PATCH /api/users/{user_id}`: at least one of the two fields, each absent one left as it is. An
 * `email` of `""` clears it (and the answered `User` then has no `email`); a new email follows the
 * same rules as a created one.
 */
export interface UserUpdateRequest {
  name?: string;
  email?: string;
}

const userName = str({ minLength: 1, maxLength: USER_NAME_MAX_CHARS, pattern: USER_NAME_PATTERN });
const userEmail = str({ maxLength: USER_EMAIL_MAX_CHARS, pattern: USER_EMAIL_PATTERN });
const userPhotoUrl = str({ pattern: USER_PHOTO_URL_PATTERN });

/** The update request's `email` (the schemas' `anyOf`): a new address, or the `""` that clears it. */
const emailOrCleared: Decoder<string> = (value, path) => (value === "" ? "" : userEmail(value, path));

export const decodeUser: Decoder<User> = object(
  { id, name: userName },
  { email: userEmail, photo_url: userPhotoUrl },
);

export const decodeUsersListResponse: Decoder<UsersListResponse> = object({ users: array(decodeUser) });

export const decodeUserCreateRequest: Decoder<UserCreateRequest> = object({ name: userName }, { email: userEmail });

export const decodeUserUpdateRequest: Decoder<UserUpdateRequest> = refine(
  object({}, { name: userName, email: emailOrCleared }),
  (request) =>
    request.name === undefined && request.email === undefined
      ? "a user update carries at least one of `name` or `email`"
      : null,
);
