import { decodeUsersListResponse, type User } from "../protocol";

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
