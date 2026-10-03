"""The users of the vault: one folder each under `users/`, and the profile that says who they are.

A user is a directory, `users/<user-id>/`, holding `profile.json` and everything that student
writes: their subjects, and inside them the topics, sessions, sources, notes and study material of
the layout in `docs/modules/vault.md`. The profile is the whole of what this module knows about
them -- the id that names the folder, the name the clients call them by, an email and a photo, both
optional, and when they were added -- and it is what says the user exists at all: `Vault.for_user`
reads nothing else, so a folder without one is a folder this backend refuses to write into.

Two rules follow from the folder being the identity. The id is derived from the name the way a
subject's slug is derived from its own -- `slugify`, with the first free numeric suffix when another
user already has it -- and then never changes: editing a name rewrites `profile.json` and leaves the
folder alone, because every subject, session and notes tag under it is named after the path it
lives at (ADR-0002), and moving a user would orphan the history of their notes. And the folder is
created whole, `subjects/` and its `.gitkeep` included, so a user another PC clones from GitHub is
a user that PC can write into: git tracks no empty directory, and a profile on its own would arrive
as a folder with nowhere to put a first subject.

What is refused is refused before anything is written, in Spanish, because the message is what the
«Editar perfil» screen shows the student (#553, #555): a name that is empty or absurdly long, an
email that is not one, an id that is not a slug -- so a value that came from a URL cannot name a
directory outside `users/` -- a `profile.json` that is there but cannot be read back, and an upload
that is not an image this backend can decode. No refusal leaves a half-created user behind, and a
refused photo leaves the profile and the folder exactly as they were.

The photo is the one part of a profile that is not text. `set_user_photo` takes the bytes a client
uploaded with the media type it declares, and stores them as `users/<id>/photo.jpg`: decoded by
OpenCV, shrunk so its long edge is at most `USER_PHOTO_LONG_EDGE_PX` (never enlarged) and
re-encoded as a JPEG at `USER_PHOTO_JPEG_QUALITY`. One format under one name is what keeps
`profile.photo` a file name and not a path -- a 12-megapixel phone selfie becomes a few tens of
kilobytes in the repository every PC clones and pushes, and no client has to remember what the
upload was. Decoding, shrinking and encoding are CPU-bound, and the atomic writers fsync, so the
photo calls are blocking in the way the capture processing is: a server caller runs them in a
worker thread (`asyncio.to_thread`).

This module builds its paths from `vault.root`, not from `vault.path`: users belong to the
repository, not to one user's content, so a user handle lists and edits profiles exactly as a root
one does. `create_user`, which adds somebody to the vault rather than editing somebody, is the root
handle's alone. Nothing here runs git; the sync commits what these writers leave behind.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from pydantic import ValidationError

from studentassistant.vault.errors import UserError, UserNotFoundError
from studentassistant.vault.files import write_bytes_atomic, write_json_atomic, write_text_atomic
from studentassistant.vault.models import VaultFileModel
from studentassistant.vault.slugs import is_slug, slugify, unique_slug
from studentassistant.vault.subjects import SUBJECTS_DIRNAME
from studentassistant.vault.vault import USER_PROFILE_NAME, USERS_DIRNAME, Vault

# What a profile may hold, so a paste into the name field cannot write a megabyte of it into every
# listing and every avatar menu. 254 is the longest address the RFCs allow.
MAX_USER_NAME_CHARACTERS = 80
MAX_USER_EMAIL_CHARACTERS = 254
# One `@`, a dot in the domain and no whitespace: enough to catch a typo, little enough to refuse
# no address anybody actually has.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# git tracks no empty directory, so a freshly created user's `subjects/` carries this to survive a
# clone; nothing reads it and nothing else in the vault is named after it.
GITKEEP_NAME = ".gitkeep"

# The photo is one file with one name, in the user's own folder: `profile.photo` holds that name
# for the clients, and no other one is ever written, read or served.
USER_PHOTO_NAME = "photo.jpg"
# The media types an upload may declare, which are the three OpenCV decodes from a phone, a
# browser's file picker and a screenshot alike (`vault/sources.py` keeps the same three for a
# pasted image).
USER_PHOTO_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_USER_PHOTO_BYTES = 5 * 1024 * 1024
"""The largest upload accepted as a photo, before it is downscaled and re-encoded."""
USER_PHOTO_LONG_EDGE_PX = 512
"""The long edge of the stored photo: an avatar, not a portrait to print."""
USER_PHOTO_JPEG_QUALITY = 85


class UserProfileError(UserError, ValueError):
    """A name, an email or a photo this backend refuses to put in a profile; the message is
    Spanish, for the student whose it is."""


class UserFileError(UserError):
    """`profile.json` is there but holds something this backend cannot read as a `UserProfile`.

    Distinct from `UserNotFoundError`, which is what a folder without one gives: this one says the
    vault's own content is damaged, which is the student's to recover, not a wrong id to retry.
    """


class UserProfile(VaultFileModel):
    """`users/<id>/profile.json`: who the user is, and which photo the clients show of them.

    `email` and `photo` are `None` until the student gives one; `photo` is then the file's name
    (`photo.jpg`) next to this one, never a path or a URL. `id` is the folder's name and never
    changes, whatever `name` is edited into.
    """

    id: str
    name: str
    email: str | None = None
    photo: str | None = None
    created_at: datetime


def create_user(vault: Vault, name: str, email: str | None = None) -> UserProfile:
    """Add a user to the vault: `users/<id>/profile.json` and the `subjects/` folder beside it.

    The id is `slugify(name)` with the first free numeric suffix when another user already has it,
    so two students called "Ana García" are two folders and neither one's notes are written over
    the other's. The name is stored trimmed, as it is validated, and the id derived from it stays
    theirs for as long as the vault exists.

    Raises:
        ValueError: when `vault` is a user handle. Adding somebody is a decision about the whole
            repository, and a user's own folder holds no `users/` to add them under.
        UserProfileError: when `name` or `email` is not one this backend accepts; nothing is
            written and no folder is created.
        FileExistsError: when the directory of the id that was picked is there already, which is
            what creating the same user twice at once looks like; nothing is overwritten.
        OSError: when a directory cannot be made or a file cannot be written.
    """
    if vault.user_id is not None:
        raise ValueError(
            f"this handle is the one of user {vault.user_id!r}: create_user needs a root handle,"
            " the one Vault.open returns"
        )
    validated_name = _validated_name(name)
    validated_email = _validated_email(email)
    user_id = unique_slug(_id_from_name(validated_name), user_ids(vault))
    directory = _user_directory(vault, user_id)
    directory.mkdir(parents=True)
    profile = UserProfile(
        id=user_id,
        name=validated_name,
        email=validated_email,
        created_at=datetime.now(UTC),
    )
    write_json_atomic(directory / USER_PROFILE_NAME, profile)
    subjects = directory / SUBJECTS_DIRNAME
    subjects.mkdir()
    write_text_atomic(subjects / GITKEEP_NAME, "")
    return profile


def list_users(vault: Vault) -> list[UserProfile]:
    """Every user of the vault, by name and then id, so a listing never depends on the file system.

    Names are compared case-insensitively and by nothing else: the selection screen reads "ana" and
    "Ana" as the same place in the alphabet, which is how a student looks for a classmate in it. An
    accented vowel therefore sorts by its code point, after every plain letter, rather than next to
    the letter it decorates -- this backend has no collation table, and a screen that wants one
    re-sorts what it is given. A vault with no user yet has no `users/` directory either, and lists
    as empty.

    Raises:
        UserFileError: when a folder under `users/` has no `profile.json` this backend can read
            back; the user stays an error instead of quietly vanishing from the selection screen,
            because the vault holds the only copy of everything under them.
    """
    profiles = (get_user(vault, user_id) for user_id in user_ids(vault))
    return sorted(profiles, key=lambda profile: (profile.name.casefold(), profile.id))


def get_user(vault: Vault, user_id: str) -> UserProfile:
    """Read the profile of `user_id`, which is the whole of `users/<user-id>/profile.json`.

    Raises:
        UserNotFoundError: when `user_id` is not a slug, refused before any path is built or
            looked at, or when `users/<user-id>/profile.json` is not there -- a folder without one
            is not a user, which is what `Vault.for_user` says of it too.
        UserFileError: when the file is there but cannot be read, is not JSON, or holds fields that
            are not a `UserProfile`.
    """
    if not is_slug(user_id):
        raise UserNotFoundError(f"{user_id!r} is not a user id")
    profile_path = _user_directory(vault, user_id) / USER_PROFILE_NAME
    if not profile_path.is_file():
        raise UserNotFoundError(
            f"the vault at {vault.root} has no user {user_id!r}: {profile_path} is not there"
        )
    return _read_profile(profile_path)


def update_user(
    vault: Vault, user_id: str, *, name: str | None = None, email: str | None = None
) -> UserProfile:
    """Edit a user's `name` and `email`, keeping their id, their photo and their `created_at`.

    A field left at `None` is kept as it is; an `email` of `""` (or of nothing but whitespace) is
    cleared, which is how the profile screen says "no address" without a separate control for it.
    The name is stored trimmed and the id is left alone: it named the folder before the edit and
    still names it after, with every subject and notes tag under it. Nothing is rewritten when the
    profile comes out the same. The user is looked up before the fields are validated, so an id
    that is not there is `UserNotFoundError` whatever comes with it.

    Raises:
        UserNotFoundError, UserFileError: as `get_user`.
        UserProfileError: when a `name` or an `email` given is not one this backend accepts; the
            file keeps the profile it had.
        OSError: when the file cannot be written.
    """
    profile = get_user(vault, user_id)
    changes: dict[str, str | None] = {}
    if name is not None:
        changes["name"] = _validated_name(name)
    if email is not None:
        changes["email"] = _validated_email(email)
    if not changes:
        return profile
    updated = profile.model_copy(update=changes)
    if updated == profile:
        return profile
    write_json_atomic(_user_directory(vault, user_id) / USER_PROFILE_NAME, updated)
    return updated


def set_user_photo(vault: Vault, user_id: str, content: bytes, content_type: str) -> UserProfile:
    """Store `content` as the user's `photo.jpg` and say so in their profile.

    The bytes are the upload as it arrived and `content_type` the media type it declared, one of
    `USER_PHOTO_CONTENT_TYPES`. What lands in the vault is not what arrived: OpenCV decodes it (an
    upload is refused when it cannot), the image is shrunk to a long edge of
    `USER_PHOTO_LONG_EDGE_PX` -- never enlarged, so a smaller one keeps its size -- and the result
    is written as a JPEG of quality `USER_PHOTO_JPEG_QUALITY` through the secret guard and the
    atomic writer, in the user's own folder. A photo already there is replaced, and
    `profile.photo` becomes `USER_PHOTO_NAME`; the rest of the profile is untouched.

    The image is written before the profile that names it, so a write that fails halfway leaves a
    profile with no photo rather than a photo no profile mentions: a client that follows the
    profile never asks for a file that is not there.

    Raises:
        UserNotFoundError, UserFileError: as `get_user`; nothing is written.
        UserProfileError: when `content_type` is not one of the three, `content` is empty or over
            `MAX_USER_PHOTO_BYTES`, or it is not an image OpenCV can decode; nothing is written
            and the profile keeps the photo it had.
        SecretRefused: when the JPEG to store matches a secret pattern (`files.py`); neither it nor
            the profile is written.
        OSError: when a file cannot be written.
    """
    profile = get_user(vault, user_id)
    jpeg = _photo_jpeg(content, content_type)
    directory = _user_directory(vault, user_id)
    write_bytes_atomic(directory / USER_PHOTO_NAME, jpeg)
    updated = profile.model_copy(update={"photo": USER_PHOTO_NAME})
    if updated != profile:
        write_json_atomic(directory / USER_PROFILE_NAME, updated)
    return updated


def remove_user_photo(vault: Vault, user_id: str) -> UserProfile:
    """Delete the user's `photo.jpg` and take the photo out of their profile.

    Removing what is not there is not an error: the clients' «Quitar foto» says the profile has no
    photo, which is already true, and a repeated request then gets the same answer instead of a
    `404` nobody can act on. The file goes first and the profile is rewritten only when it named
    one, so a user with no photo is left with the `profile.json` bytes they had.

    Raises:
        UserNotFoundError, UserFileError: as `get_user`; nothing is deleted.
        OSError: when the file or the profile cannot be written.
    """
    profile = get_user(vault, user_id)
    directory = _user_directory(vault, user_id)
    (directory / USER_PHOTO_NAME).unlink(missing_ok=True)
    if profile.photo is None:
        return profile
    updated = profile.model_copy(update={"photo": None})
    write_json_atomic(directory / USER_PROFILE_NAME, updated)
    return updated


def read_user_photo(vault: Vault, user_id: str) -> bytes | None:
    """The stored `photo.jpg` of `user_id`, or `None` when they have no photo.

    The profile decides: a user whose `photo` is `None` has no photo whatever is in their folder,
    and one whose `photo` names a file this module never writes has none either, so a hand-edited
    profile cannot point a route at another path in the vault. `None` is also what a profile that
    names a photo the folder has lost gives -- a clone that dropped the image, or a file deleted
    behind this backend's back -- because "no photo to show" is what the client can act on and an
    error is not.

    Raises:
        UserNotFoundError, UserFileError: as `get_user`.
        OSError: when the file is there but cannot be read.
    """
    profile = get_user(vault, user_id)
    if profile.photo != USER_PHOTO_NAME:
        return None
    path = _user_directory(vault, user_id) / USER_PHOTO_NAME
    if not path.is_file():
        return None
    return path.read_bytes()


def user_ids(vault: Vault) -> list[str]:
    """The sorted ids of the folders under `users/`, without reading any `profile.json`.

    Every folder counts, including one whose profile is missing or unreadable: it is still a user
    the student may recover, and a new one taking its id would write over them. A caller that must
    survive one broken profile -- the selection screen, the migration -- lists these and reads each
    one itself.
    """
    root = vault.root / USERS_DIRNAME
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def _user_directory(vault: Vault, user_id: str) -> Path:
    """Where `user_id`'s folder lives, whether or not anything has written it yet.

    Under `root`, not under `path`: a user handle's own `users/` does not exist, and the users of a
    vault are the same ones whichever handle asks.
    """
    return vault.root / USERS_DIRNAME / user_id


def _id_from_name(name: str) -> str:
    """The id a new user called `name` would get, before the suffix that makes it theirs alone.

    Raises:
        UserProfileError: when the name has no letter and no digit in it, so there is no slug to
            name a folder after.
    """
    try:
        return slugify(name)
    except ValueError as error:
        raise UserProfileError(
            f"El nombre «{name}» no sirve para identificar a un usuario: necesita al menos una"
            " letra o un número."
        ) from error


def _validated_name(name: str) -> str:
    """`name` trimmed, and refused in Spanish when nothing of it is left or it is too long."""
    trimmed = name.strip()
    if not trimmed:
        raise UserProfileError("El nombre no puede estar vacío.")
    if len(trimmed) > MAX_USER_NAME_CHARACTERS:
        raise UserProfileError(
            f"El nombre es demasiado largo: tiene {len(trimmed)} caracteres y el máximo es"
            f" {MAX_USER_NAME_CHARACTERS}."
        )
    return trimmed


def _validated_email(email: str | None) -> str | None:
    """`email` trimmed, `None` when there is none to store, and refused in Spanish when it is not
    one.

    An empty address is not an invalid one: it is the student clearing the field, which stores
    `None` -- the profile says it can hold no address, and an empty string in it would be a second
    way of spelling that.
    """
    if email is None:
        return None
    trimmed = email.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_USER_EMAIL_CHARACTERS:
        raise UserProfileError(
            f"La dirección de correo es demasiado larga: tiene {len(trimmed)} caracteres y el"
            f" máximo es {MAX_USER_EMAIL_CHARACTERS}."
        )
    if _EMAIL.fullmatch(trimmed) is None:
        raise UserProfileError(
            f"La dirección de correo «{trimmed}» no es válida: escríbela como nombre@dominio.com."
        )
    return trimmed


def _photo_jpeg(content: bytes, content_type: str) -> bytes:
    """`content` as the JPEG the vault stores: validated, decoded, downscaled, re-encoded.

    This is the image work `sources/captures.py` does on a photographed page, written here again
    because the dependency runs the other way (`sources` imports `vault`, never the reverse), and
    a writer of the vault cannot reach into a feature module for a helper.

    Raises:
        UserProfileError: when the media type is not one a photo may declare, nothing was
            uploaded, the upload is over `MAX_USER_PHOTO_BYTES`, or OpenCV cannot decode it.
        RuntimeError: when OpenCV cannot encode an image it has just decoded.
    """
    if content_type not in USER_PHOTO_CONTENT_TYPES:
        raise UserProfileError(
            f"El tipo de imagen «{content_type}» no se acepta como foto de perfil: usa JPEG, PNG"
            " o WebP."
        )
    if not content:
        raise UserProfileError("La foto está vacía: elige un fichero de imagen.")
    if len(content) > MAX_USER_PHOTO_BYTES:
        raise UserProfileError(
            f"La foto es demasiado grande: ocupa {len(content) // 1024} KiB y el máximo es"
            f" {MAX_USER_PHOTO_BYTES // (1024 * 1024)} MiB."
        )
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise UserProfileError(
            "El fichero no se puede leer como una imagen: guárdalo como JPEG, PNG o WebP y vuelve"
            " a intentarlo."
        )
    encoded_ok, encoded = cv2.imencode(
        ".jpg",
        _downscaled_photo(image),
        [cv2.IMWRITE_JPEG_QUALITY, USER_PHOTO_JPEG_QUALITY],
    )
    if not encoded_ok:  # pragma: no cover - OpenCV encodes any 8-bit BGR image
        raise RuntimeError("OpenCV could not encode the photo as JPEG")
    return encoded.tobytes()


def _downscaled_photo(image: np.ndarray) -> np.ndarray:
    """`image` shrunk so its long edge is at most `USER_PHOTO_LONG_EDGE_PX`; never enlarged.

    A reduction is averaged, not sampled (`INTER_AREA`), so what survives at avatar size is still
    recognisable; an image already that small is returned as it is, because enlarging it would
    invent pixels and bytes for nothing.
    """
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= USER_PHOTO_LONG_EDGE_PX:
        return image
    scale = USER_PHOTO_LONG_EDGE_PX / longest
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def _read_profile(profile_path: Path) -> UserProfile:
    """Read `profile.json`, naming in the error which of the ways it can be wrong it is.

    Raises:
        UserFileError: when the file cannot be read, is not JSON, or is not a `UserProfile`.
    """
    try:
        text = profile_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise UserFileError(f"{profile_path} cannot be read: {error}") from error
    try:
        return UserProfile.model_validate(json.loads(text))
    except json.JSONDecodeError as error:
        raise UserFileError(
            f"{profile_path} is not JSON this backend can parse: {error}"
        ) from error
    except ValidationError as error:
        raise UserFileError(f"{profile_path} does not hold a user profile: {error}") from error
