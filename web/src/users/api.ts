import { decodeUser, decodeUsersListResponse, type User, type UserCreateRequest, type UserUpdateRequest } from "../protocol";

export type UsersResult = { kind: "ok"; users: User[] } | { kind: "error" };

/** `GET /api/users`: every user of the vault (the selection screen's cards). */
export async function listUsers(): Promise<UsersResult> {
  try {
    const response = await fetch("/api/users");
    if (!response.ok) return { kind: "error" };
    return { kind: "ok", users: decodeUsersListResponse(await response.json(), "").users };
  } catch {
    return { kind: "error" };
  }
}

export type UserWriteResult =
  | { kind: "ok"; user: User }
  | { kind: "rejected"; status: number; detail: string | null }
  | { kind: "error" };

async function detailOf(response: Response): Promise<string | null> {
  try {
    const body: unknown = await response.json();
    const detail = (body as { detail?: unknown } | null)?.detail;
    return typeof detail === "string" ? detail : null;
  } catch {
    return null;
  }
}

async function userRequest(path: string, init: RequestInit): Promise<UserWriteResult> {
  try {
    const response = await fetch(path, init);
    if (!response.ok) return { kind: "rejected", status: response.status, detail: await detailOf(response) };
    return { kind: "ok", user: decodeUser(await response.json(), "") };
  } catch {
    return { kind: "error" };
  }
}

/** `POST /api/users`: a new profile (the server picks its id). */
export function createUser(request: UserCreateRequest): Promise<UserWriteResult> {
  return userRequest("/api/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
}

/** `PATCH /api/users/{id}`: only the fields given change; an `email` of `""` clears it. */
export function updateUser(id: string, changes: UserUpdateRequest): Promise<UserWriteResult> {
  return userRequest(`/api/users/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

/** `PUT /api/users/{id}/photo`: the raw image with its own `Content-Type`. */
export function uploadUserPhoto(id: string, file: Blob): Promise<UserWriteResult> {
  return userRequest(`/api/users/${encodeURIComponent(id)}/photo`, {
    method: "PUT",
    headers: { "Content-Type": file.type },
    body: file,
  });
}

/** `DELETE /api/users/{id}/photo`: answers the user without `photo_url`. */
export function deleteUserPhoto(id: string): Promise<UserWriteResult> {
  return userRequest(`/api/users/${encodeURIComponent(id)}/photo`, { method: "DELETE" });
}
