"""The profile photo: what an upload becomes in the vault, and what a client reads back."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from user_helpers import add_user, everything_under, user_directory

from studentassistant.vault import (
    SecretRefused,
    UserNotFoundError,
    UserProfile,
    UserProfileError,
    Vault,
    create_user,
    get_user,
    read_user_photo,
    remove_user_photo,
    set_user_photo,
)
from studentassistant.vault.users import (
    MAX_USER_PHOTO_BYTES,
    USER_PHOTO_CONTENT_TYPES,
    USER_PHOTO_JPEG_QUALITY,
    USER_PHOTO_LONG_EDGE_PX,
    USER_PHOTO_NAME,
)
from studentassistant.vault.vault import USER_PROFILE_NAME

NAME = "Ana García"
EMAIL = "ana.garcia@instituto.es"
# The size a phone's selfie has, and the shape the assertions below can do arithmetic on.
BIG_WIDTH, BIG_HEIGHT = 1200, 900


def photo_file(vault: Vault, user_id: str) -> Path:
    """The user's `photo.jpg`, whether or not anything has written it yet."""
    return user_directory(vault, user_id) / USER_PHOTO_NAME


def profile_file(vault: Vault, user_id: str) -> Path:
    """The user's `profile.json`, whether or not anything has written it yet."""
    return user_directory(vault, user_id) / USER_PROFILE_NAME


def drawn(width: int, height: int, seed: int = 7) -> np.ndarray:
    """A synthetic portrait: a gradient with a face-ish shape on it, drawn and never photographed.

    A gradient and a little noise, so a downscale has something to average and two `seed`s give
    two images that are not the same bytes.
    """
    rng = np.random.default_rng(seed)
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 0] = np.linspace(60, 220, height, dtype=np.uint8)[:, None]
    image[:, :, 2] = np.linspace(200, 40, width, dtype=np.uint8)[None, :]
    cv2.circle(image, (width // 2, height // 3), min(width, height) // 5, (230, 200, 60), -1)
    cv2.rectangle(image, (width // 3, 2 * height // 3), (2 * width // 3, height), (40, 40, 40), -1)
    noise = rng.integers(0, 8, image.shape)
    return np.clip(image.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def encoded(image: np.ndarray, extension: str) -> bytes:
    """`image` as the bytes an upload of that format carries."""
    # The quality parameter is JPEG's alone: OpenCV warns about it on the other two.
    parameters = [cv2.IMWRITE_JPEG_QUALITY, 95] if extension == ".jpg" else []
    ok, buffer = cv2.imencode(extension, image, parameters)
    assert ok, "the test's own image must encode"
    return buffer.tobytes()


def decoded(data: bytes) -> np.ndarray:
    """The image `data` holds; a test that cannot decode what it stored has nothing to assert."""
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def set_profile_photo(vault: Vault, user_id: str, photo: str | None) -> None:
    """Hand-edit `profile.json`'s `photo`, the way a damaged or a hand-made vault would."""
    contents = json.loads(profile_file(vault, user_id).read_text(encoding="utf-8"))
    contents["photo"] = photo
    profile_file(vault, user_id).write_text(json.dumps(contents, indent=2) + "\n", encoding="utf-8")


def test_an_upload_becomes_a_downscaled_jpeg_beside_the_profile(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME, email=EMAIL)
    upload = encoded(drawn(BIG_WIDTH, BIG_HEIGHT), ".png")

    profile = set_user_photo(tmp_vault, created.id, upload, "image/png")

    stored = photo_file(tmp_vault, created.id).read_bytes()
    image = decoded(stored)
    assert profile.photo == USER_PHOTO_NAME
    assert (profile.id, profile.name, profile.email) == (created.id, NAME, EMAIL)
    assert profile.created_at == created.created_at
    assert stored.startswith(b"\xff\xd8"), "one format in the vault, whatever arrived"
    assert max(image.shape[:2]) == USER_PHOTO_LONG_EDGE_PX
    assert (image.shape[1], image.shape[0]) == (512, 384), "the shape it had, only smaller"
    assert len(stored) < len(upload), "a phone's photo is not the repository's business"
    assert get_user(tmp_vault, created.id) == profile
    assert everything_under(user_directory(tmp_vault, created.id)) == [
        Path(USER_PHOTO_NAME),
        Path(USER_PROFILE_NAME),
        Path("subjects"),
        Path("subjects") / ".gitkeep",
    ], "no temporary file is left beside the two"


def test_the_stored_photo_is_the_downscaled_upload_at_quality_85(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)
    upload = encoded(drawn(BIG_WIDTH, BIG_HEIGHT), ".png")

    set_user_photo(tmp_vault, created.id, upload, "image/png")

    # The writer's own arithmetic spelled out: 1200x900 down to a 512 long edge, JPEG quality 85.
    reference = cv2.imencode(
        ".jpg",
        cv2.resize(decoded(upload), (512, 384), interpolation=cv2.INTER_AREA),
        [cv2.IMWRITE_JPEG_QUALITY, USER_PHOTO_JPEG_QUALITY],
    )[1].tobytes()

    assert read_user_photo(tmp_vault, created.id) == reference


def test_a_photo_already_smaller_than_the_limit_keeps_its_size(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)

    set_user_photo(tmp_vault, created.id, encoded(drawn(200, 120), ".png"), "image/png")

    stored = read_user_photo(tmp_vault, created.id)
    assert stored is not None
    assert (decoded(stored).shape[1], decoded(stored).shape[0]) == (200, 120), "never enlarged"


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [
        ("image/jpeg", ".jpg"),
        ("image/png", ".png"),
        ("image/webp", ".webp"),
    ],
)
def test_every_accepted_format_is_stored_as_the_same_jpeg(
    tmp_vault: Vault, content_type: str, extension: str
) -> None:
    assert content_type in USER_PHOTO_CONTENT_TYPES
    created = create_user(tmp_vault, NAME)
    upload = encoded(drawn(700, 500), extension)

    profile = set_user_photo(tmp_vault, created.id, upload, content_type)

    stored = photo_file(tmp_vault, created.id).read_bytes()
    assert profile.photo == USER_PHOTO_NAME
    assert stored.startswith(b"\xff\xd8")
    assert max(decoded(stored).shape[:2]) == USER_PHOTO_LONG_EDGE_PX
    assert read_user_photo(tmp_vault, created.id) == stored


def test_a_second_photo_replaces_the_first_and_leaves_the_profile_alone(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)
    set_user_photo(tmp_vault, created.id, encoded(drawn(800, 600, seed=1), ".png"), "image/png")
    profile_bytes = profile_file(tmp_vault, created.id).read_bytes()

    profile = set_user_photo(
        tmp_vault, created.id, encoded(drawn(300, 300, seed=2), ".png"), "image/png"
    )

    stored = decoded(photo_file(tmp_vault, created.id).read_bytes())
    assert (stored.shape[1], stored.shape[0]) == (300, 300), "the second photo, not a blend"
    assert profile.photo == USER_PHOTO_NAME
    assert profile_file(tmp_vault, created.id).read_bytes() == profile_bytes
    assert everything_under(user_directory(tmp_vault, created.id)) == [
        Path(USER_PHOTO_NAME),
        Path(USER_PROFILE_NAME),
        Path("subjects"),
        Path("subjects") / ".gitkeep",
    ]


@pytest.mark.parametrize(
    "content_type",
    ["image/gif", "image/svg+xml", "image/tiff", "application/octet-stream", "text/plain", ""],
)
def test_a_media_type_that_is_not_one_of_the_three_is_refused(
    tmp_vault: Vault, content_type: str
) -> None:
    created = create_user(tmp_vault, NAME)
    upload = encoded(drawn(80, 60), ".png")

    with pytest.raises(UserProfileError) as refusal:
        set_user_photo(tmp_vault, created.id, upload, content_type)

    assert "no se acepta como foto" in refusal.value.args[0]
    assert not photo_file(tmp_vault, created.id).exists()
    assert get_user(tmp_vault, created.id).photo is None


def test_an_empty_upload_is_refused(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)

    with pytest.raises(UserProfileError) as refusal:
        set_user_photo(tmp_vault, created.id, b"", "image/jpeg")

    assert "vacía" in refusal.value.args[0]


def test_an_upload_over_the_limit_is_refused_in_spanish(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)
    too_large = b"\x00" * (MAX_USER_PHOTO_BYTES + 1)

    with pytest.raises(UserProfileError) as refusal:
        set_user_photo(tmp_vault, created.id, too_large, "image/jpeg")

    assert "demasiado grande" in refusal.value.args[0]
    assert "5 MiB" in refusal.value.args[0]
    assert not photo_file(tmp_vault, created.id).exists()


def test_an_upload_of_exactly_the_limit_is_refused_as_an_image_not_as_a_size(
    tmp_vault: Vault,
) -> None:
    created = create_user(tmp_vault, NAME)

    with pytest.raises(UserProfileError) as refusal:
        set_user_photo(tmp_vault, created.id, b"\x00" * MAX_USER_PHOTO_BYTES, "image/jpeg")

    assert "demasiado grande" not in refusal.value.args[0]


@pytest.mark.parametrize(
    "content",
    [
        b"esto no es una imagen",
        b"\x89PNG\r\n\x1a\n",  # a PNG's signature and nothing else
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
    ],
)
def test_content_that_is_not_an_image_is_refused(tmp_vault: Vault, content: bytes) -> None:
    created = create_user(tmp_vault, NAME)

    with pytest.raises(UserProfileError) as refusal:
        set_user_photo(tmp_vault, created.id, content, "image/png")

    assert "no se puede leer como una imagen" in refusal.value.args[0]
    assert not photo_file(tmp_vault, created.id).exists()


def test_a_photo_cut_short_is_refused(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)
    truncated = encoded(drawn(BIG_WIDTH, BIG_HEIGHT), ".jpg")[:1024]

    with pytest.raises(UserProfileError):
        set_user_photo(tmp_vault, created.id, truncated, "image/jpeg")

    assert not photo_file(tmp_vault, created.id).exists()


def test_a_refused_photo_leaves_the_photo_and_the_profile_it_found(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME, email=EMAIL)
    set_user_photo(tmp_vault, created.id, encoded(drawn(400, 300), ".png"), "image/png")
    photo_bytes = photo_file(tmp_vault, created.id).read_bytes()
    before = everything_under(tmp_vault.root)

    with pytest.raises(UserProfileError):
        set_user_photo(tmp_vault, created.id, b"no es una imagen", "image/gif")
    with pytest.raises(UserProfileError):
        set_user_photo(tmp_vault, created.id, b"no es una imagen", "image/png")

    assert photo_file(tmp_vault, created.id).read_bytes() == photo_bytes
    assert get_user(tmp_vault, created.id) == created.model_copy(update={"photo": USER_PHOTO_NAME})
    assert everything_under(tmp_vault.root) == before


def test_a_first_photo_that_is_refused_writes_nothing_at_all(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)
    before = everything_under(tmp_vault.root)

    with pytest.raises(UserProfileError):
        set_user_photo(tmp_vault, created.id, b"no es una imagen", "image/png")

    assert everything_under(tmp_vault.root) == before


def test_a_photo_the_secret_guard_refuses_leaves_a_profile_that_does_not_name_it(
    tmp_vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image is written first and the profile second, so a refused write names no photo.

    The guard itself is `files.py`'s and has its own tests; what is pinned here is the order the
    two writes happen in, which is what keeps `profile.photo` from pointing at a file that is not
    there.
    """
    from studentassistant.vault import users as users_module

    created = create_user(tmp_vault, NAME, email=EMAIL)
    profile_bytes = profile_file(tmp_vault, created.id).read_bytes()

    def refuse(path: Path, content: bytes) -> None:
        raise SecretRefused("anthropic-api-key")

    monkeypatch.setattr(users_module, "write_bytes_atomic", refuse)

    with pytest.raises(SecretRefused):
        set_user_photo(tmp_vault, created.id, encoded(drawn(80, 60), ".png"), "image/png")

    assert profile_file(tmp_vault, created.id).read_bytes() == profile_bytes
    assert not photo_file(tmp_vault, created.id).exists()


def test_removing_a_photo_deletes_the_file_and_clears_the_profile(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME, email=EMAIL)
    set_user_photo(tmp_vault, created.id, encoded(drawn(600, 400), ".png"), "image/png")

    profile = remove_user_photo(tmp_vault, created.id)

    assert profile.photo is None
    assert (profile.id, profile.name, profile.email) == (created.id, NAME, EMAIL)
    assert profile.created_at == created.created_at
    assert not photo_file(tmp_vault, created.id).exists()
    assert read_user_photo(tmp_vault, created.id) is None
    assert get_user(tmp_vault, created.id) == profile
    assert (
        json.loads(profile_file(tmp_vault, created.id).read_text(encoding="utf-8"))["photo"] is None
    )


def test_removing_a_photo_that_is_not_there_changes_nothing(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)
    profile_bytes = profile_file(tmp_vault, created.id).read_bytes()
    before = everything_under(tmp_vault.root)

    assert remove_user_photo(tmp_vault, created.id) == created

    assert profile_file(tmp_vault, created.id).read_bytes() == profile_bytes
    assert everything_under(tmp_vault.root) == before


def test_removing_a_photo_and_setting_it_again_is_a_photo(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)
    upload = encoded(drawn(400, 400), ".png")
    set_user_photo(tmp_vault, created.id, upload, "image/png")
    remove_user_photo(tmp_vault, created.id)

    profile = set_user_photo(tmp_vault, created.id, upload, "image/png")

    assert profile.photo == USER_PHOTO_NAME
    assert read_user_photo(tmp_vault, created.id) is not None


def test_a_user_with_no_photo_reads_as_none(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)

    assert read_user_photo(tmp_vault, created.id) is None


def test_a_profile_that_names_a_photo_the_folder_has_lost_reads_as_none(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)
    set_profile_photo(tmp_vault, created.id, USER_PHOTO_NAME)

    assert read_user_photo(tmp_vault, created.id) is None
    assert remove_user_photo(tmp_vault, created.id).photo is None


def test_a_profile_cannot_point_the_photo_at_another_path(tmp_vault: Vault) -> None:
    """`profile.photo` is a flag, not a path: only `photo.jpg` of that user is ever served."""
    ana = create_user(tmp_vault, NAME)
    luis = create_user(tmp_vault, "Luis Martín")
    set_user_photo(tmp_vault, ana.id, encoded(drawn(200, 200), ".png"), "image/png")
    set_profile_photo(tmp_vault, luis.id, f"../{ana.id}/{USER_PHOTO_NAME}")

    assert read_user_photo(tmp_vault, luis.id) is None
    assert read_user_photo(tmp_vault, ana.id) is not None


@pytest.mark.parametrize("user_id", ["ana-garcia", "", "..", "../ana-garcia", "Ana García"])
def test_the_photo_of_a_user_that_is_not_there_is_refused(tmp_vault: Vault, user_id: str) -> None:
    upload = encoded(drawn(80, 60), ".png")

    with pytest.raises(UserNotFoundError):
        read_user_photo(tmp_vault, user_id)
    with pytest.raises(UserNotFoundError):
        remove_user_photo(tmp_vault, user_id)
    with pytest.raises(UserNotFoundError):
        set_user_photo(tmp_vault, user_id, upload, "image/png")


def test_a_photo_is_written_under_the_users_folder_whichever_handle_asks(
    tmp_vault: Vault,
) -> None:
    ana = create_user(tmp_vault, NAME)
    luis = create_user(tmp_vault, "Luis Martín")
    handle = tmp_vault.for_user(ana.id)
    upload = encoded(drawn(640, 480), ".jpg")

    profile = set_user_photo(handle, luis.id, upload, "image/jpeg")

    assert profile.photo == USER_PHOTO_NAME
    assert photo_file(tmp_vault, luis.id).is_file()
    assert not (handle.path / USER_PHOTO_NAME).exists(), "not in the handle's own folder"
    assert not (tmp_vault.root / USER_PHOTO_NAME).exists(), "and not at the repository's root"
    assert read_user_photo(handle, luis.id) == read_user_photo(tmp_vault, luis.id)
    assert remove_user_photo(handle, luis.id).photo is None
    assert not photo_file(tmp_vault, luis.id).exists()


def test_two_users_never_see_each_others_photo(tmp_vault: Vault) -> None:
    ana = create_user(tmp_vault, NAME)
    luis = create_user(tmp_vault, "Luis Martín")
    set_user_photo(tmp_vault, ana.id, encoded(drawn(200, 200, seed=1), ".png"), "image/png")
    set_user_photo(tmp_vault, luis.id, encoded(drawn(300, 100, seed=2), ".png"), "image/png")

    ana_photo = read_user_photo(tmp_vault, ana.id)
    luis_photo = read_user_photo(tmp_vault, luis.id)

    assert ana_photo is not None and luis_photo is not None
    assert ana_photo != luis_photo
    assert (decoded(ana_photo).shape[1], decoded(ana_photo).shape[0]) == (200, 200)
    assert (decoded(luis_photo).shape[1], decoded(luis_photo).shape[0]) == (300, 100)

    remove_user_photo(tmp_vault, ana.id)

    assert read_user_photo(tmp_vault, ana.id) is None
    assert read_user_photo(tmp_vault, luis.id) == luis_photo


def test_a_user_added_by_a_helper_reads_and_writes_its_photo(tmp_vault: Vault) -> None:
    """`add_user` writes a bare folder: a photo needs a profile and no `subjects/`."""
    user = add_user(tmp_vault, "ana-garcia", NAME)

    profile = set_user_photo(user, "ana-garcia", encoded(drawn(90, 90), ".png"), "image/png")

    assert profile.photo == USER_PHOTO_NAME
    assert isinstance(profile, UserProfile)
    assert read_user_photo(user, "ana-garcia") is not None
    assert everything_under(user_directory(tmp_vault, "ana-garcia")) == [
        Path(USER_PHOTO_NAME),
        Path(USER_PROFILE_NAME),
    ]
