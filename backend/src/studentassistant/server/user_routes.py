"""The users REST API: who this vault holds, and the profile photo of each one (protocol 1.8, #549).

`GET /api/users` is what a client's selection screen reads, `POST /api/users` adds a student, `PATCH
/api/users/{user_id}` edits a name and an email, and the three `/photo` routes serve, replace and
remove the avatar. The bodies are the five `rest.users.*` messages of `protocol/README.md` "Users
(1.8)". Adding a student from a screen is not built yet: today the maintainer's `studentassistant
users add` and this `POST` are the two ways in.

**None of these routes is user-scoped.** They are about the users themselves, so they work on the
vault's ROOT handle -- the one `SessionService.open_vault()` gives -- and never on
`vault.for_user(...)`, which is also why `vault.users` builds its paths from `vault.root`. An
`X-SA-User` header or an `sa_user` cookie on one of these requests is therefore read by nobody, and
a stale or bogus selection cannot make the selection screen refuse to list the users to choose from.
They still sit behind the bearer/loopback check like every `/api` route except health and pairing:
the selection rides on top of that trust, it does not replace it. And a write by a
cookie-authenticated device is a cross-site target, so `BearerAuthMiddleware`'s `same_site_origin`
check covers these methods exactly as it covers every other cookie write.

The photo is the one body here that is not JSON. `PUT` takes the raw image of one of
`USER_PHOTO_CONTENT_TYPES` and streams it, so what this process holds is bounded by `[server]
max_user_photo_bytes` and an upload past it is refused with 413 as it arrives rather than after; a
media type that is not one of the three is refused with 415, and the user is looked up, before the
body is read at all, so a stale id costs nobody an upload. What lands in the vault is `vault.users`'
downscaled `photo.jpg`, never the bytes that arrived, and `GET` serves that one file as
`image/jpeg` with `Cache-Control: no-cache`.

`photo_url` carries the first hex characters of the stored photo's SHA-256 as its `?v=`, so the URL
changes exactly when the image behind it does and a client that cached the previous avatar cannot
keep showing it. Every write tells the vault's sync (`SessionService.note_change`), and every vault
call runs in a worker thread -- one blocking step per request, so the event loop only waits: the
photo's decode/resize/encode is CPU-bound and the writers fsync.
"""

from __future__ import annotations

import asyncio
import hashlib

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import ValidationError
from python_multipart.multipart import parse_options_header

from studentassistant.config import ServerSettings
from studentassistant.protocol import (
    USER_PHOTO_CONTENT_TYPES,
    User,
    UserCreateRequest,
    UsersListResponse,
    UserUpdateRequest,
)
from studentassistant.server.errors import unknown_user_error
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    SecretRefused,
    UserFileError,
    UserNotFoundError,
    UserProfile,
    UserProfileError,
    Vault,
    create_user,
    get_user,
    list_users,
    read_user_photo,
    remove_user_photo,
    set_user_photo,
    update_user,
)

PHOTO_VERSION_HEX_CHARS = 12
"""How much of the stored photo's SHA-256 its `?v=` carries: plenty for one user's one file."""

USER_PHOTO_MEDIA_TYPE = "image/jpeg"
"""The one media type `GET .../photo` answers: the vault stores every upload as a JPEG."""

PHOTO_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
}
"""A browser revalidates an avatar instead of trusting its copy, and sniffs nothing about it."""

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
DAMAGED_PROFILE_DETAIL = "No se puede leer el perfil de un usuario de la bóveda."
NO_PHOTO_DETAIL = "El usuario no tiene foto de perfil."
USER_EXISTS_DETAIL = "Ya se está creando un usuario con ese nombre: vuelve a intentarlo."
SECRET_DETAIL = "La foto parece contener una clave o un token y no se ha guardado."


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _too_large(max_bytes: int) -> HTTPException:
    return HTTPException(
        status.HTTP_413_CONTENT_TOO_LARGE,
        f"La foto supera el máximo que se acepta ({_megabytes(max_bytes)}).",
    )


def _unsupported_media_type(content_type: str) -> HTTPException:
    """415 for a `Content-Type` that is not one a profile photo may declare.

    The sentence is the one `vault.users` writes for the same refusal, said here because this route
    answers before it has read a byte of the body the vault would have looked at.
    """
    return HTTPException(
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        f"El tipo de imagen «{content_type}» no se acepta como foto de perfil: usa JPEG, PNG o"
        " WebP.",
    )


def _declared_media_type(request: Request) -> str:
    """The media type the upload declares, without its parameters (a charset, a boundary)."""
    declared, _ = parse_options_header(request.headers.get("content-type"))
    return declared.decode("latin-1").strip().lower()


async def _read_photo(request: Request, max_bytes: int) -> bytes:
    """The upload's bytes, read as they stream in and refused with 413 past `max_bytes`.

    A `Content-Length` over the cap is refused before anything is read, so a client announcing a
    gigabyte never gets it into this process; the running total is what catches a body that declares
    no length, or one that lies about it.

    Raises:
        HTTPException: 413 when the body is over `max_bytes`.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise _too_large(max_bytes)
    content = bytearray()
    async for chunk in request.stream():
        content += chunk
        if len(content) > max_bytes:
            raise _too_large(max_bytes)
    return bytes(content)


async def _root_vault(request: Request) -> Vault:
    """The vault's root handle: the users of a vault belong to it, not to one user's folder.

    Raises:
        HTTPException: 503 when the vault cannot be opened at all.
    """
    sessions: SessionService = request.app.state.sessions
    try:
        return await sessions.open_vault()
    except VaultUnavailableError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
        ) from error


def _note_change(request: Request) -> None:
    """Tell the vault's `GitSync` a writer left files behind, for the background commit and push."""
    sessions: SessionService = request.app.state.sessions
    sessions.note_change()


# -- the profile as the protocol's `User` ---------------------------------------------------------


def _photo_url(vault: Vault, profile: UserProfile) -> str | None:
    """`/api/users/<id>/photo?v=<12 hex of the photo's SHA-256>`, or None when there is no photo.

    The version is of the STORED bytes, so it changes on every upload that changes the image and on
    nothing else. A profile naming a photo its folder has lost gives no URL: `read_user_photo` says
    there is none to serve, and pointing a client at a 404 is worse than an empty avatar.
    """
    if profile.photo is None:
        return None
    content = read_user_photo(vault, profile.id)
    if content is None:
        return None
    version = hashlib.sha256(content).hexdigest()[:PHOTO_VERSION_HEX_CHARS]
    return f"/api/users/{profile.id}/photo?v={version}"


def _user(vault: Vault, profile: UserProfile) -> User:
    """A vault profile as the protocol's `User`, without the `email` or `photo_url` it lacks.

    `read_user_photo` reads the profile back to decide, which is the vault's own rule about which
    file a profile may point at; this module builds no path under `users/` itself.

    Raises:
        UserNotFoundError: as `read_user_photo`.
        UserFileError: as `read_user_photo`, and for a profile that parses but is no `User` the
            protocol can carry -- a name over its 80 characters, which only a hand-edited
            `profile.json` can hold. That is damaged content like an unparsable one, and the
            student is told the same way instead of getting a body with no `detail` in it.
    """
    try:
        return User(
            id=profile.id,
            name=profile.name,
            email=profile.email,
            photo_url=_photo_url(vault, profile),
        )
    except ValidationError as error:
        raise UserFileError(f"user {profile.id}'s profile is no protocol User: {error}") from error


# -- one blocking step per route: the vault's writer and the `User` it leaves behind --------------


def _listed(vault: Vault) -> list[User]:
    """Every user as a `User`, in `list_users`' order: by name, then id."""
    return [_user(vault, profile) for profile in list_users(vault)]


def _created(vault: Vault, name: str, email: str | None) -> User:
    return _user(vault, create_user(vault, name, email))


def _updated(vault: Vault, user_id: str, name: str | None, email: str | None) -> User:
    return _user(vault, update_user(vault, user_id, name=name, email=email))


def _photo_set(vault: Vault, user_id: str, content: bytes, content_type: str) -> User:
    return _user(vault, set_user_photo(vault, user_id, content, content_type))


def _photo_removed(vault: Vault, user_id: str) -> User:
    return _user(vault, remove_user_photo(vault, user_id))


def users_router() -> APIRouter:
    """The users routes; the vault and the `[server]` photo cap are read from `app.state`."""
    router = APIRouter(prefix="/api")

    @router.get(
        "/users",
        response_model_exclude_none=True,
        responses={
            500: {"description": "A user's `profile.json` cannot be read back."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def all_users(request: Request) -> UsersListResponse:
        vault = await _root_vault(request)
        try:
            return UsersListResponse(users=await asyncio.to_thread(_listed, vault))
        except UserFileError as error:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, DAMAGED_PROFILE_DETAIL
            ) from error

    @router.post(
        "/users",
        status_code=status.HTTP_201_CREATED,
        response_model_exclude_none=True,
        responses={
            409: {"description": "The folder of the id picked for the new user is there already."},
            422: {"description": "A name or an email the vault refuses."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def add_user(request: Request, body: UserCreateRequest) -> User:
        vault = await _root_vault(request)
        try:
            created = await asyncio.to_thread(_created, vault, body.name, body.email)
        except UserProfileError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        except FileExistsError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, USER_EXISTS_DETAIL) from error
        except UserFileError as error:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, DAMAGED_PROFILE_DETAIL
            ) from error
        _note_change(request)
        return created

    @router.patch(
        "/users/{user_id}",
        response_model_exclude_none=True,
        responses={
            404: {"description": "Unknown user (`user_not_found`)."},
            422: {"description": "A name or an email the vault refuses."},
            500: {"description": "The user's `profile.json` cannot be read back."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def edit_user(request: Request, user_id: str, body: UserUpdateRequest) -> User:
        vault = await _root_vault(request)
        try:
            # An absent field keeps what the user has and an `email` of `""` clears it: both are
            # `update_user`'s own reading of these two values, so the body is passed straight on.
            updated = await asyncio.to_thread(_updated, vault, user_id, body.name, body.email)
        except UserNotFoundError as error:
            raise unknown_user_error(user_id) from error
        except UserProfileError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        except UserFileError as error:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, DAMAGED_PROFILE_DETAIL
            ) from error
        _note_change(request)
        return updated

    @router.put(
        "/users/{user_id}/photo",
        response_model_exclude_none=True,
        responses={
            404: {"description": "Unknown user (`user_not_found`)."},
            413: {"description": "The upload is over `[server] max_user_photo_bytes`."},
            415: {"description": "A `Content-Type` that is not one a photo may declare."},
            422: {"description": "Not an image this backend can decode, or one it refuses."},
            500: {"description": "The user's `profile.json` cannot be read back."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def put_photo(request: Request, user_id: str) -> User:
        limits: ServerSettings = request.app.state.server
        vault = await _root_vault(request)
        content_type = _declared_media_type(request)
        if content_type not in USER_PHOTO_CONTENT_TYPES:
            raise _unsupported_media_type(content_type)
        try:
            # Before the body: an id this vault does not have is a 404 whatever arrives with it.
            await asyncio.to_thread(get_user, vault, user_id)
        except UserNotFoundError as error:
            raise unknown_user_error(user_id) from error
        except UserFileError as error:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, DAMAGED_PROFILE_DETAIL
            ) from error
        content = await _read_photo(request, limits.max_user_photo_bytes)
        try:
            updated = await asyncio.to_thread(_photo_set, vault, user_id, content, content_type)
        except UserProfileError as error:
            # No image, an empty body, or one over `vault.users.MAX_USER_PHOTO_BYTES`: that ceiling
            # is the vault's own, so a `[server] max_user_photo_bytes` raised past it lets the bytes
            # by the 413 above and the student gets this Spanish 422 naming the vault's 5 MiB.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        except SecretRefused as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, SECRET_DETAIL) from error
        except UserFileError as error:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, DAMAGED_PROFILE_DETAIL
            ) from error
        _note_change(request)
        return updated

    @router.delete(
        "/users/{user_id}/photo",
        response_model_exclude_none=True,
        responses={
            404: {"description": "Unknown user (`user_not_found`)."},
            500: {"description": "The user's `profile.json` cannot be read back."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def delete_photo(request: Request, user_id: str) -> User:
        vault = await _root_vault(request)
        try:
            # Removing a photo that is not there is not an error: «Quitar foto» says the profile
            # has none, which is already true, so a repeated request gets the same answer.
            updated = await asyncio.to_thread(_photo_removed, vault, user_id)
        except UserNotFoundError as error:
            raise unknown_user_error(user_id) from error
        except UserFileError as error:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, DAMAGED_PROFILE_DETAIL
            ) from error
        _note_change(request)
        return updated

    @router.get(
        "/users/{user_id}/photo",
        responses={
            404: {"description": "Unknown user (`user_not_found`), or a user with no photo."},
            500: {"description": "The user's `profile.json` cannot be read back."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def get_photo(request: Request, user_id: str) -> Response:
        vault = await _root_vault(request)
        try:
            content = await asyncio.to_thread(read_user_photo, vault, user_id)
        except UserNotFoundError as error:
            raise unknown_user_error(user_id) from error
        except UserFileError as error:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, DAMAGED_PROFILE_DETAIL
            ) from error
        if content is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NO_PHOTO_DETAIL)
        return Response(content, media_type=USER_PHOTO_MEDIA_TYPE, headers=dict(PHOTO_HEADERS))

    return router


__all__ = [
    "PHOTO_HEADERS",
    "PHOTO_VERSION_HEX_CHARS",
    "USER_PHOTO_MEDIA_TYPE",
    "users_router",
]
