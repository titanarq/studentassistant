"""`server.user_routes`: the users REST API, its photo and the trust it sits behind.

Every route is walked over a real `create_app` app -- middlewares, error handler, session service --
and a real vault, so what is asserted is the body and the status a client gets, and the bytes the
vault is left with. The active-user rule itself is `test_user_scope.py`'s; what is checked here is
that these routes are NOT under it, that a cookie write is still a cross-site target, and that the
photo's cap, its media types and its `?v=` behave as `protocol/README.md` "Users (1.8)" says.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import studentassistant.server.user_routes as user_routes
from conftest import LAN_HOST, LOCAL_BASE_URL, LOOPBACK_HOST, PUBLIC_URL, HostedTestClient
from studentassistant.config import (
    DEFAULT_MAX_USER_PHOTO_BYTES,
    ObserverSettings,
    ServerSettings,
    Settings,
)
from studentassistant.protocol import USER_COOKIE, USER_HEADER
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.user_routes import (
    DAMAGED_PROFILE_DETAIL,
    NO_PHOTO_DETAIL,
    USER_EXISTS_DETAIL,
    VAULT_UNAVAILABLE_DETAIL,
)
from studentassistant.vault import UserProfileError, Vault, create_user, set_user_photo, user_ids
from studentassistant.vault.users import USER_PHOTO_LONG_EDGE_PX
from studentassistant.vault.vault import USER_PROFILE_NAME, USERS_DIRNAME

FIRST_USER = "ana-garcia"
"""The user `tests/conftest.py`'s `tmp_vault` is born with (`slugify("Ana García")`, #548)."""

FIRST_NAME = "Ana García"
CLASSMATE = "lucia-fernandez"
CLASSMATE_NAME = "Lucía Fernández"
CLASSMATE_EMAIL = "lucia.fernandez@instituto.es"
NOBODY = "nadie"
BEARER_DETAIL = "a valid bearer token is required"
CROSS_SITE_DETAIL = "a cross-site request cannot use the session cookie"
OWN_ORIGIN = PUBLIC_URL
"""The `Origin` a page of this backend carries: the LAN client here is served from `PUBLIC_URL`."""

AppFactory = Callable[..., FastAPI]


def photo(width: int = 40, height: int = 30, seed: int = 7, extension: str = ".png") -> bytes:
    """A synthetic portrait's bytes, in the format named: an upload OpenCV decodes.

    Two `seed`s are two different images, so a test can tell one upload from the next by the `?v=`
    the route answers with.
    """
    rng = np.random.default_rng(seed)
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 0] = np.linspace(60, 220, height, dtype=np.uint8)[:, None]
    image[:, :, 2] = np.linspace(200, 40, width, dtype=np.uint8)[None, :]
    noise = rng.integers(0, 12, image.shape)
    image = np.clip(image.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    parameters = [cv2.IMWRITE_JPEG_QUALITY, 95] if extension == ".jpg" else []
    encoded_ok, buffer = cv2.imencode(extension, image, parameters)
    assert encoded_ok
    return buffer.tobytes()


def decoded(content: bytes) -> np.ndarray:
    """The image `content` holds, so a test can measure what the vault stored."""
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, "the bytes are no image OpenCV can read"
    return image


def user_directory(vault: Vault, user_id: str) -> Path:
    """Where `user_id`'s folder lives: the place a route's writes must land in."""
    return vault.root / USERS_DIRNAME / user_id


def stored_photo(vault: Vault, user_id: str) -> Path:
    """The user's `photo.jpg`, whether or not anything has written it yet."""
    return user_directory(vault, user_id) / "photo.jpg"


def profile(vault: Vault, user_id: str) -> dict[str, Any]:
    """`users/<id>/profile.json` as it is on disk, which is what these routes are a window on."""
    return json.loads(
        (user_directory(vault, user_id) / USER_PROFILE_NAME).read_text(encoding="utf-8")
    )


def put_photo(
    client: TestClient,
    content: bytes,
    content_type: str = "image/png",
    user_id: str = FIRST_USER,
    headers: dict[str, str] | None = None,
) -> Any:
    """`PUT /api/users/{user_id}/photo` with a raw body, as the protocol's photo upload travels."""
    return client.put(
        f"/api/users/{user_id}/photo",
        content=content,
        headers={**(headers or {}), "Content-Type": content_type},
    )


# -- the app and its clients ---------------------------------------------------------------------


@pytest.fixture
def make_app(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> AppFactory:
    """An app over `tmp_vault`; keyword arguments become that app's `[server]` overrides."""

    def make(**limits: Any) -> FastAPI:
        return create_app(
            static_dir=tmp_path / "no-web-build",
            server=server.model_copy(update=limits),
            codes=codes,
            vault=tmp_vault,
        )

    return make


@pytest.fixture
def app(make_app: AppFactory) -> FastAPI:
    return make_app()


def client_of(app: FastAPI, host: str = LOOPBACK_HOST) -> TestClient:
    """A TestClient over `app` whose requests come from `host` (as conftest's `client_at`)."""
    base_url = LOCAL_BASE_URL if host == LOOPBACK_HOST else PUBLIC_URL
    return HostedTestClient(app, base_url=base_url, client=(host, 50000))


@pytest.fixture
def local(app: FastAPI) -> TestClient:
    """The PC itself: trusted without a token while `[server] trust_localhost`."""
    return client_of(app)


@pytest.fixture
def lan(app: FastAPI) -> TestClient:
    """A LAN client with no token of its own: what the bearer middleware lets in or not."""
    return client_of(app, LAN_HOST)


@pytest.fixture
def classmate(tmp_vault: Vault) -> Vault:
    """`tmp_vault` with a second user: a vault the selection screen has a choice on."""
    assert create_user(tmp_vault, CLASSMATE_NAME, CLASSMATE_EMAIL).id == CLASSMATE
    assert user_ids(tmp_vault) == [FIRST_USER, CLASSMATE]
    return tmp_vault


@pytest.fixture
def paired_token(local: TestClient, lan: TestClient) -> str:
    """A device paired with this very app, and the bearer token it was given."""
    code = local.post("/api/pair/codes").json()["code"]
    response = lan.post(
        "/api/pair",
        json={
            "pairing_code": code,
            "device_name": "Móvil de Lucía",
            "client_kind": "android",
            "protocol_version": "1.8",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def token_cookie(token: str) -> dict[str, str]:
    """The headers of a request a WebView makes: the token as a cookie, not as a header."""
    return {"Cookie": f"sa_token={token}"}


# -- GET /api/users ------------------------------------------------------------------------------


def test_the_list_is_the_users_the_vault_holds(local: TestClient, tmp_vault: Vault) -> None:
    response = local.get("/api/users")

    assert response.status_code == 200
    # `email` and `photo_url` are absent, never null: the protocol's `User` is strict.
    assert response.json() == {"users": [{"id": FIRST_USER, "name": FIRST_NAME}]}


def test_the_list_is_by_name_and_carries_what_a_user_has(
    local: TestClient, classmate: Vault
) -> None:
    put_photo(local, photo(), user_id=CLASSMATE)

    response = local.get("/api/users")

    assert response.status_code == 200
    users = response.json()["users"]
    assert [user["id"] for user in users] == [FIRST_USER, CLASSMATE]
    assert users[0] == {"id": FIRST_USER, "name": FIRST_NAME}
    assert users[1]["email"] == CLASSMATE_EMAIL
    assert users[1]["photo_url"].startswith(f"/api/users/{CLASSMATE}/photo?v=")


def test_a_vault_with_no_user_lists_empty(make_app: AppFactory, vault_with_no_users: Vault) -> None:
    response = client_of(make_app()).get("/api/users")

    assert response.status_code == 200
    assert response.json() == {"users": []}


def test_a_profile_this_backend_cannot_read_is_500(local: TestClient, tmp_vault: Vault) -> None:
    (user_directory(tmp_vault, FIRST_USER) / USER_PROFILE_NAME).write_text(
        '{"id": "ana-garcia"', encoding="utf-8"
    )

    response = local.get("/api/users")

    assert response.status_code == 500
    assert response.json() == {"detail": DAMAGED_PROFILE_DETAIL}


def test_a_profile_that_is_no_user_the_protocol_can_carry_is_500(
    local: TestClient, tmp_vault: Vault
) -> None:
    # A `profile.json` only a hand edit can produce: it parses, but no `User` carries a name over
    # 80 characters, so the route cannot answer with it. Damaged content, said in Spanish, rather
    # than an unhandled error and a 500 with no `detail` in it.
    written = profile(tmp_vault, FIRST_USER)
    written["name"] = "A" * 81
    (user_directory(tmp_vault, FIRST_USER) / USER_PROFILE_NAME).write_text(
        json.dumps(written), encoding="utf-8"
    )

    listing = local.get("/api/users")

    assert listing.status_code == 500
    assert listing.json() == {"detail": DAMAGED_PROFILE_DETAIL}


def test_a_missing_vault_is_503(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, isolated_config: Path
) -> None:
    isolated_config.write_text(f'[vault]\npath = "{tmp_path / "nowhere"}"\n', encoding="utf-8")
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        llm_settings=Settings(
            vault={"path": tmp_path / "nowhere"}, observer=ObserverSettings(enabled=False)
        ),
    )

    response = client_of(app).get("/api/users")

    assert response.status_code == 503
    assert response.json() == {"detail": VAULT_UNAVAILABLE_DETAIL}


# -- POST /api/users -----------------------------------------------------------------------------


def test_a_created_user_is_answered_and_listed(local: TestClient, tmp_vault: Vault) -> None:
    # A name travels trimmed: the protocol refuses the whitespace, the form trims it first.
    response = local.post("/api/users", json={"name": CLASSMATE_NAME, "email": CLASSMATE_EMAIL})

    assert response.status_code == 201, response.text
    assert response.json() == {"id": CLASSMATE, "name": CLASSMATE_NAME, "email": CLASSMATE_EMAIL}
    # What is created is the vault's own structure, not a row in a table.
    assert user_ids(tmp_vault) == [FIRST_USER, CLASSMATE]
    assert profile(tmp_vault, CLASSMATE)["name"] == CLASSMATE_NAME
    assert (user_directory(tmp_vault, CLASSMATE) / "subjects").is_dir()
    assert [user["id"] for user in local.get("/api/users").json()["users"]] == [
        FIRST_USER,
        CLASSMATE,
    ]


def test_a_user_created_without_an_email_has_none(local: TestClient, tmp_vault: Vault) -> None:
    response = local.post("/api/users", json={"name": CLASSMATE_NAME})

    assert response.status_code == 201
    assert response.json() == {"id": CLASSMATE, "name": CLASSMATE_NAME}
    assert profile(tmp_vault, CLASSMATE)["email"] is None


def test_users_of_one_name_each_get_an_id_of_their_own(local: TestClient, tmp_vault: Vault) -> None:
    first = local.post("/api/users", json={"name": FIRST_NAME})

    second = local.post("/api/users", json={"name": FIRST_NAME})

    assert first.status_code == 201 and second.status_code == 201
    assert [first.json()["id"], second.json()["id"]] == [f"{FIRST_USER}-2", f"{FIRST_USER}-3"]
    assert user_ids(tmp_vault) == [FIRST_USER, f"{FIRST_USER}-2", f"{FIRST_USER}-3"]


def test_a_name_that_is_no_slug_is_refused_in_spanish(local: TestClient, tmp_vault: Vault) -> None:
    with pytest.raises(UserProfileError) as vault_says:
        create_user(tmp_vault, "???")

    response = local.post("/api/users", json={"name": "???"})

    assert response.status_code == 422
    # The vault's Spanish sentence, as the profile screen shows it: not a validation list.
    assert response.json() == {"detail": str(vault_says.value)}
    assert user_ids(tmp_vault) == [FIRST_USER]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"name": ""}, id="an-empty-name"),
        pytest.param({"name": "   "}, id="a-blank-name"),
        pytest.param({"name": "A" * 81}, id="a-name-over-80-characters"),
        pytest.param({"name": " Ana "}, id="a-name-that-is-not-trimmed"),
        pytest.param({"name": "Ana", "email": "no-es-un-correo"}, id="an-email-that-is-not-one"),
        pytest.param({"name": "Ana", "sobran": 1}, id="an-unknown-field"),
        pytest.param({"email": "ana@instituto.es"}, id="no-name"),
        pytest.param({}, id="an-empty-body"),
    ],
)
def test_a_body_the_protocol_refuses_is_422(
    local: TestClient, tmp_vault: Vault, body: dict[str, Any]
) -> None:
    response = local.post("/api/users", json=body)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert user_ids(tmp_vault) == [FIRST_USER]


def test_a_creation_that_finds_the_folder_there_already_is_409(
    local: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def taken(*args: Any, **kwargs: Any) -> Any:
        # What two clients adding "Ana García" in the same instant look like from in here: the id
        # was free when it was picked and its directory is there by the time it is written.
        raise FileExistsError("users/ana-garcia-2 is there already")

    monkeypatch.setattr(user_routes, "create_user", taken)

    response = local.post("/api/users", json={"name": FIRST_NAME})

    assert response.status_code == 409
    assert response.json() == {"detail": USER_EXISTS_DETAIL}


# -- PATCH /api/users/{user_id} ------------------------------------------------------------------


def test_a_profile_is_edited_and_keeps_its_id(local: TestClient, tmp_vault: Vault) -> None:
    response = local.patch(
        f"/api/users/{FIRST_USER}", json={"name": "Ana M. García", "email": "ana@instituto.es"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": FIRST_USER,
        "name": "Ana M. García",
        "email": "ana@instituto.es",
    }
    # The id named the folder before the edit and still names it after: nothing moved.
    assert user_ids(tmp_vault) == [FIRST_USER]
    assert profile(tmp_vault, FIRST_USER)["name"] == "Ana M. García"


def test_a_field_the_body_leaves_out_is_kept(local: TestClient, tmp_vault: Vault) -> None:
    local.patch(f"/api/users/{FIRST_USER}", json={"email": "ana@instituto.es"})

    response = local.patch(f"/api/users/{FIRST_USER}", json={"name": "Ana Belén"})

    assert response.status_code == 200
    assert response.json() == {"id": FIRST_USER, "name": "Ana Belén", "email": "ana@instituto.es"}


def test_an_empty_email_clears_it(local: TestClient, tmp_vault: Vault) -> None:
    local.patch(f"/api/users/{FIRST_USER}", json={"email": "ana@instituto.es"})

    response = local.patch(f"/api/users/{FIRST_USER}", json={"email": ""})

    assert response.status_code == 200
    assert response.json() == {"id": FIRST_USER, "name": FIRST_NAME}
    assert profile(tmp_vault, FIRST_USER)["email"] is None


def test_an_edit_that_changes_nothing_is_answered_all_the_same(
    local: TestClient, tmp_vault: Vault
) -> None:
    response = local.patch(f"/api/users/{FIRST_USER}", json={"name": FIRST_NAME})

    assert response.status_code == 200
    assert response.json() == {"id": FIRST_USER, "name": FIRST_NAME}


def test_an_edit_the_vault_refuses_is_422_in_spanish(
    local: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The protocol's own model refuses an over-long or untrimmed name before the vault sees it
    # (that is `test_an_edit_body_the_protocol_refuses_is_422`), so what is proved here is the
    # mapping itself: the vault stays the authority on a profile, and its Spanish sentence is what
    # reaches the wire rather than a 500.
    refusal = UserProfileError("El nombre no puede estar vacío.")

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise refusal

    monkeypatch.setattr(user_routes, "update_user", refused)

    response = local.patch(f"/api/users/{FIRST_USER}", json={"name": "Ana"})

    assert response.status_code == 422
    assert response.json() == {"detail": str(refusal)}


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"email": "no"}, {"sobran": 1}])
def test_an_edit_body_the_protocol_refuses_is_422(
    local: TestClient, tmp_vault: Vault, body: dict[str, Any]
) -> None:
    response = local.patch(f"/api/users/{FIRST_USER}", json=body)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert profile(tmp_vault, FIRST_USER)["name"] == FIRST_NAME


# -- unknown users -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("patch", f"/api/users/{NOBODY}", id="patch"),
        pytest.param("put", f"/api/users/{NOBODY}/photo", id="put-photo"),
        pytest.param("delete", f"/api/users/{NOBODY}/photo", id="delete-photo"),
        pytest.param("get", f"/api/users/{NOBODY}/photo", id="get-photo"),
    ],
)
def test_an_unknown_user_is_404_user_not_found(
    local: TestClient, tmp_vault: Vault, method: str, path: str
) -> None:
    response = local.request(
        method,
        path,
        json={"name": FIRST_NAME} if method == "patch" else None,
        headers={"Content-Type": "image/png"} if method == "put" else None,
        content=b"x" if method == "put" else None,
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": f"El usuario «{NOBODY}» no existe en esta bóveda.",
        "code": "user_not_found",
    }


@pytest.mark.parametrize("user_id", ["Ana", "ana.garcia", "ANA-GARCIA", "ana_garcia"])
def test_an_id_that_is_no_slug_is_the_same_404(
    local: TestClient, tmp_vault: Vault, user_id: str
) -> None:
    # The path is not pattern-checked, so the vault's own rule decides: a stale cookie, a typo and
    # a user another PC removed all look the same to the client, and none of them can name a path.
    response = local.patch(f"/api/users/{user_id}", json={"name": "Da igual"})

    assert response.status_code == 404
    assert response.json()["code"] == "user_not_found"


# -- the photo -----------------------------------------------------------------------------------


def test_a_photo_goes_in_as_a_jpeg_and_comes_back_out(local: TestClient, tmp_vault: Vault) -> None:
    upload = photo()

    response = put_photo(local, upload)

    assert response.status_code == 200, response.text
    user = response.json()
    assert user["id"] == FIRST_USER
    assert user["name"] == FIRST_NAME
    assert user["photo_url"].startswith(f"/api/users/{FIRST_USER}/photo?v=")
    # What the vault holds is not what arrived: one file, one name, a JPEG.
    stored = stored_photo(tmp_vault, FIRST_USER).read_bytes()
    assert stored != upload
    assert stored.startswith(b"\xff\xd8\xff")
    assert profile(tmp_vault, FIRST_USER)["photo"] == "photo.jpg"

    served = local.get(user["photo_url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert served.headers["cache-control"] == "no-cache"
    assert served.content == stored


def test_the_photo_url_is_the_stored_bytes_own_hash(local: TestClient, tmp_vault: Vault) -> None:
    user = put_photo(local, photo()).json()

    version = user["photo_url"].split("?v=", 1)[1]
    stored = stored_photo(tmp_vault, FIRST_USER).read_bytes()

    assert version == hashlib.sha256(stored).hexdigest()[:12]
    assert len(version) == 12


def test_the_photo_url_changes_with_the_photo(local: TestClient, tmp_vault: Vault) -> None:
    first = put_photo(local, photo(seed=1)).json()["photo_url"]
    first_bytes = stored_photo(tmp_vault, FIRST_USER).read_bytes()

    second = put_photo(local, photo(seed=2, width=64, height=48)).json()["photo_url"]

    assert first != second
    assert first_bytes != stored_photo(tmp_vault, FIRST_USER).read_bytes()
    # The `?v=` is a cache key, not a selector: a client still holding the old URL is a client
    # whose cached copy a browser would have to revalidate, and the route serves the photo there is.
    assert local.get(first).content == local.get(second).content


def test_a_big_photo_is_stored_as_an_avatar(local: TestClient, tmp_vault: Vault) -> None:
    upload = photo(width=1200, height=900, extension=".jpg")

    response = put_photo(local, upload, "image/jpeg")

    assert response.status_code == 200, response.text
    stored = stored_photo(tmp_vault, FIRST_USER).read_bytes()
    height, width = decoded(stored).shape[:2]
    assert max(height, width) == USER_PHOTO_LONG_EDGE_PX
    assert len(stored) < len(upload)


def test_replacing_a_photo_leaves_one_file(local: TestClient, tmp_vault: Vault) -> None:
    put_photo(local, photo(seed=1))

    put_photo(local, photo(seed=2), "image/jpeg")

    assert sorted(path.name for path in user_directory(tmp_vault, FIRST_USER).iterdir()) == [
        "photo.jpg",
        "profile.json",
        "subjects",
    ]


def test_a_photo_is_removed_and_its_user_has_none(local: TestClient, tmp_vault: Vault) -> None:
    put_photo(local, photo())

    response = local.delete(f"/api/users/{FIRST_USER}/photo")

    assert response.status_code == 200
    assert response.json() == {"id": FIRST_USER, "name": FIRST_NAME}
    assert not stored_photo(tmp_vault, FIRST_USER).exists()
    assert profile(tmp_vault, FIRST_USER)["photo"] is None
    missing = local.get(f"/api/users/{FIRST_USER}/photo")
    assert missing.status_code == 404
    assert missing.json() == {"detail": NO_PHOTO_DETAIL}


def test_removing_a_photo_that_is_not_there_is_not_an_error(
    local: TestClient, tmp_vault: Vault
) -> None:
    response = local.delete(f"/api/users/{FIRST_USER}/photo")

    assert response.status_code == 200
    assert response.json() == {"id": FIRST_USER, "name": FIRST_NAME}


def test_a_user_with_no_photo_is_served_no_photo(local: TestClient, tmp_vault: Vault) -> None:
    response = local.get(f"/api/users/{FIRST_USER}/photo")

    assert response.status_code == 404
    assert response.json() == {"detail": NO_PHOTO_DETAIL}


def test_a_profile_naming_a_photo_the_folder_lost_is_a_user_without_one(
    local: TestClient, tmp_vault: Vault
) -> None:
    put_photo(local, photo())
    stored_photo(tmp_vault, FIRST_USER).unlink()

    assert local.get(f"/api/users/{FIRST_USER}/photo").status_code == 404
    assert "photo_url" not in local.get("/api/users").json()["users"][0]


@pytest.mark.parametrize(
    "content_type",
    [
        pytest.param("application/pdf", id="not-an-image"),
        pytest.param("image/gif", id="an-image-type-that-is-not-one-of-the-three"),
        pytest.param("text/plain", id="text"),
        pytest.param("", id="nothing-declared"),
    ],
)
def test_a_content_type_that_is_not_one_of_the_three_is_415(
    local: TestClient, tmp_vault: Vault, content_type: str
) -> None:
    response = put_photo(local, photo(), content_type)

    assert response.status_code == 415
    assert response.json() == {
        "detail": f"El tipo de imagen «{content_type}» no se acepta como foto de perfil: usa JPEG,"
        " PNG o WebP."
    }
    assert not stored_photo(tmp_vault, FIRST_USER).exists()
    assert profile(tmp_vault, FIRST_USER)["photo"] is None


def test_a_content_type_carries_its_parameters(local: TestClient, tmp_vault: Vault) -> None:
    response = put_photo(local, photo(), "image/png; charset=binary")

    assert response.status_code == 200, response.text


def test_an_upload_that_is_no_image_is_422_in_spanish(local: TestClient, tmp_vault: Vault) -> None:
    with pytest.raises(UserProfileError) as vault_says:
        set_user_photo(tmp_vault, FIRST_USER, b"esto no es una imagen", "image/png")

    response = put_photo(local, b"esto no es una imagen")

    assert response.status_code == 422
    assert response.json() == {"detail": str(vault_says.value)}
    assert profile(tmp_vault, FIRST_USER)["photo"] is None


def test_an_empty_upload_is_422(local: TestClient, tmp_vault: Vault) -> None:
    response = put_photo(local, b"", "image/jpeg")

    assert response.status_code == 422
    assert response.json() == {"detail": "La foto está vacía: elige un fichero de imagen."}


def test_an_upload_over_the_cap_is_413(make_app: AppFactory, tmp_vault: Vault) -> None:
    cap = 1024 * 1024
    client = client_of(make_app(max_user_photo_bytes=cap))
    upload = b"x" * (cap + 1)

    response = put_photo(client, upload)

    assert response.status_code == 413
    assert response.json() == {"detail": "La foto supera el máximo que se acepta (1.0 MB)."}
    assert not stored_photo(tmp_vault, FIRST_USER).exists()
    assert profile(tmp_vault, FIRST_USER)["photo"] is None


def test_a_chunked_upload_over_the_cap_is_413_as_it_streams(
    make_app: AppFactory, tmp_vault: Vault
) -> None:
    client = client_of(make_app(max_user_photo_bytes=32))
    upload = photo(width=200, height=200)
    assert len(upload) > 32
    chunks: Iterable[bytes] = (upload[index : index + 16] for index in range(0, len(upload), 16))

    # No `Content-Length` to believe: the running total is what refuses this one.
    response = client.put(
        f"/api/users/{FIRST_USER}/photo", content=chunks, headers={"Content-Type": "image/png"}
    )

    assert response.status_code == 413
    assert not stored_photo(tmp_vault, FIRST_USER).exists()


def test_the_cap_is_a_server_setting(make_app: AppFactory) -> None:
    assert DEFAULT_MAX_USER_PHOTO_BYTES == 5 * 1024 * 1024
    assert ServerSettings().max_user_photo_bytes == DEFAULT_MAX_USER_PHOTO_BYTES
    assert make_app(max_user_photo_bytes=1024).state.server.max_user_photo_bytes == 1024


# -- every write tells the vault's sync ----------------------------------------------------------


def test_every_write_tells_the_vaults_sync(app: FastAPI, tmp_vault: Vault) -> None:
    client = client_of(app)
    changes: list[None] = []
    # `SessionService.note_change` is the one call a route makes into `GitSync.note_change`.
    app.state.sessions._note_change = lambda: changes.append(None)  # type: ignore[method-assign]

    assert client.post("/api/users", json={"name": CLASSMATE_NAME}).status_code == 201
    assert client.patch(f"/api/users/{CLASSMATE}", json={"name": "Lucía F."}).status_code == 200
    assert put_photo(client, photo(), user_id=CLASSMATE).status_code == 200
    assert client.delete(f"/api/users/{CLASSMATE}/photo").status_code == 200

    assert len(changes) == 4
    # The reads are not writes: they tell the sync nothing.
    assert client.get("/api/users").status_code == 200
    assert client.get(f"/api/users/{CLASSMATE}/photo").status_code == 404
    assert len(changes) == 4


def test_a_refused_write_tells_the_sync_nothing(app: FastAPI, tmp_vault: Vault) -> None:
    client = client_of(app)
    changes: list[None] = []
    app.state.sessions._note_change = lambda: changes.append(None)  # type: ignore[method-assign]

    assert client.post("/api/users", json={"name": "???"}).status_code == 422
    assert client.patch(f"/api/users/{NOBODY}", json={"name": "Nadie"}).status_code == 404
    assert put_photo(client, photo(), "text/plain").status_code == 415
    assert put_photo(client, b"no es una imagen").status_code == 422

    assert changes == []


# -- these routes are not user-scoped, and are still behind the bearer check ---------------------


def test_a_bogus_user_selection_changes_nothing_here(local: TestClient, classmate: Vault) -> None:
    # `/api/users*` is about the users, so it resolves none of them: the selection screen has to
    # work with a cookie that went stale, and with no selection at all.
    headers = {USER_HEADER: NOBODY, "Cookie": f"{USER_COOKIE}={NOBODY}"}

    assert local.get("/api/users", headers=headers).status_code == 200
    created = local.post("/api/users", json={"name": "Nueva"}, headers=headers)
    assert created.status_code == 201
    edited = local.patch(
        f"/api/users/{created.json()['id']}", json={"name": "Nueva Alumno"}, headers=headers
    )
    assert edited.status_code == 200
    assert put_photo(local, photo(), headers=headers).status_code == 200
    assert local.get(f"/api/users/{FIRST_USER}/photo", headers=headers).status_code == 200
    assert local.delete(f"/api/users/{FIRST_USER}/photo", headers=headers).status_code == 200
    assert user_ids(classmate) == [FIRST_USER, CLASSMATE, "nueva"]


def test_a_lan_client_with_no_token_is_refused(lan: TestClient) -> None:
    response = lan.get("/api/users")

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}
    assert lan.post("/api/users", json={"name": CLASSMATE_NAME}).status_code == 401
    assert put_photo(lan, photo()).status_code == 401
    assert lan.delete(f"/api/users/{FIRST_USER}/photo").status_code == 401


def test_a_paired_device_with_a_token_sees_the_same_users(
    lan: TestClient, paired_token: str, classmate: Vault
) -> None:
    response = lan.get("/api/users", headers={"Authorization": f"Bearer {paired_token}"})

    assert response.status_code == 200
    assert [user["id"] for user in response.json()["users"]] == [FIRST_USER, CLASSMATE]


WRITE_ROUTES = [
    pytest.param("post", "/api/users", id="create"),
    pytest.param("patch", f"/api/users/{FIRST_USER}", id="update"),
    pytest.param("put", f"/api/users/{FIRST_USER}/photo", id="put-photo"),
    pytest.param("delete", f"/api/users/{FIRST_USER}/photo", id="delete-photo"),
]


@pytest.mark.parametrize(("method", "path"), WRITE_ROUTES)
def test_a_cookie_write_from_another_site_is_403(
    lan: TestClient, paired_token: str, tmp_vault: Vault, method: str, path: str
) -> None:
    # `sa_user` is no credential, but the `sa_token` riding beside it is: a users write carried by
    # that cookie is exactly the cross-site request `same_site_origin` exists to refuse.
    for origin in ("https://evil.example", "http://192.168.1.99:8765", "null"):
        response = lan.request(
            method, path, headers={**token_cookie(paired_token), "Origin": origin}, content=b"x"
        )
        assert response.status_code == 403, origin
        assert response.json() == {"detail": CROSS_SITE_DETAIL}

    without_origin = lan.request(method, path, headers=token_cookie(paired_token), content=b"x")
    assert without_origin.status_code == 403


@pytest.mark.parametrize(("method", "path"), WRITE_ROUTES)
def test_a_cookie_write_from_this_backends_own_page_is_allowed(
    lan: TestClient, paired_token: str, tmp_vault: Vault, method: str, path: str
) -> None:
    body = {"name": CLASSMATE_NAME} if method == "post" else {"name": FIRST_NAME}
    headers = {**token_cookie(paired_token), "Origin": OWN_ORIGIN}
    if method == "put":
        headers["Content-Type"] = "image/png"

    response = lan.request(
        method,
        path,
        headers=headers,
        json=None if method in ("put", "delete") else body,
        content=photo() if method == "put" else None,
    )

    assert response.status_code in (200, 201), response.text


def test_a_bearer_write_needs_no_origin(
    lan: TestClient, paired_token: str, tmp_vault: Vault
) -> None:
    # The native app has no browser origin to prove and needs none: it carries the token itself.
    response = lan.post(
        "/api/users",
        headers={"Authorization": f"Bearer {paired_token}"},
        json={"name": CLASSMATE_NAME},
    )

    assert response.status_code == 201, response.text
