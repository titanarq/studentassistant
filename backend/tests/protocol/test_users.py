"""The users bodies of protocol 1.8 (#545): field rules, constants and the two new error codes.

`test_examples.py` already round-trips every example against its schema and its model; this file
pins what the shared examples cannot show on their own -- what each side refuses -- and does it
through the schema and the model together, so the two never drift apart.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from studentassistant.protocol import (
    ERROR_CODES_SINCE,
    USER_COOKIE,
    USER_EMAIL_MAX_CHARS,
    USER_ERROR_CODES_SINCE,
    USER_HEADER,
    USER_NAME_MAX_CHARS,
    USER_PHOTO_CONTENT_TYPES,
    ErrorCode,
    User,
    model_for,
)

PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
LIST_RESPONSE = "rest.users.list.response"
CREATE_REQUEST = "rest.users.create.request"
CREATE_RESPONSE = "rest.users.create.response"
UPDATE_REQUEST = "rest.users.update.request"
USER_BODY = {"id": "laura-mendez", "name": "Laura Méndez"}


def _schema(name: str) -> dict[str, Any]:
    return json.loads((PROTOCOL_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def _refused(name: str, body: dict[str, Any]) -> None:
    """The schema and the model both refuse this body."""
    assert not Draft202012Validator(_schema(name)).is_valid(body)
    with pytest.raises(ValidationError):
        model_for(name).model_validate(body)


def _accepted(name: str, body: dict[str, Any]) -> None:
    """The schema and the model both take this body."""
    Draft202012Validator(_schema(name)).validate(body)
    model_for(name).model_validate(body)


def test_a_user_with_an_unknown_field_is_refused() -> None:
    """A `User` is strict in both responses and in the list: no field this side does not know."""
    tampered = {**USER_BODY, "role": "admin"}
    _refused(CREATE_RESPONSE, tampered)
    _refused("rest.users.update.response", tampered)
    _refused(LIST_RESPONSE, {"users": [tampered]})


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t"])
def test_a_blank_user_name_is_refused(blank: str) -> None:
    _refused(CREATE_REQUEST, {"name": blank})
    _refused(UPDATE_REQUEST, {"name": blank})
    _refused(CREATE_RESPONSE, {**USER_BODY, "name": blank})
    _refused(LIST_RESPONSE, {"users": [{**USER_BODY, "name": blank}]})


@pytest.mark.parametrize("untrimmed", [" Laura", "Laura ", " Laura Méndez "])
def test_a_user_name_travels_trimmed(untrimmed: str) -> None:
    _refused(CREATE_RESPONSE, {**USER_BODY, "name": untrimmed})


def test_a_user_name_is_at_most_80_characters() -> None:
    _accepted(CREATE_RESPONSE, {**USER_BODY, "name": "a" * USER_NAME_MAX_CHARS})
    _refused(CREATE_RESPONSE, {**USER_BODY, "name": "a" * (USER_NAME_MAX_CHARS + 1)})


def test_an_email_is_at_most_254_characters() -> None:
    longest = "a" * (USER_EMAIL_MAX_CHARS - len("@example.com"))
    _accepted(CREATE_RESPONSE, {**USER_BODY, "email": f"{longest}@example.com"})
    _refused(CREATE_RESPONSE, {**USER_BODY, "email": f"a{longest}@example.com"})


@pytest.mark.parametrize(
    "email",
    [
        "laura@example",
        "laura.example.com",
        "laura@ example.com",
        "laura@@example.com",
        "@example.com",
    ],
)
def test_a_malformed_email_is_refused(email: str) -> None:
    _refused(CREATE_RESPONSE, {**USER_BODY, "email": email})
    _refused(CREATE_REQUEST, {"name": "Laura Méndez", "email": email})


def test_an_empty_email_clears_it_and_only_in_the_update_request() -> None:
    _accepted(UPDATE_REQUEST, {"email": ""})
    _refused(CREATE_REQUEST, {"name": "Laura Méndez", "email": ""})
    _refused(CREATE_RESPONSE, {**USER_BODY, "email": ""})
    _refused(LIST_RESPONSE, {"users": [{**USER_BODY, "email": ""}]})


@pytest.mark.parametrize("field", ["email", "photo_url"])
def test_a_field_the_user_does_not_have_is_absent_and_never_null(field: str) -> None:
    assert not Draft202012Validator(_schema(CREATE_RESPONSE)).is_valid({**USER_BODY, field: None})
    assert field not in User.model_validate(USER_BODY).model_dump(mode="json", exclude_none=True)


def test_an_update_carries_at_least_one_field() -> None:
    _refused(UPDATE_REQUEST, {})
    _accepted(UPDATE_REQUEST, {"name": "Laura Méndez Ruiz"})
    _accepted(UPDATE_REQUEST, {"email": "laura.mendez@example.com"})


@pytest.mark.parametrize("photo_url", ["/api/users/laura-mendez/photo", "/api/users/laura-mendez"])
def test_a_photo_url_is_a_path_of_the_users_api(photo_url: str) -> None:
    _accepted(CREATE_RESPONSE, {**USER_BODY, "photo_url": photo_url})


@pytest.mark.parametrize(
    "photo_url",
    ["https://example.com/photo.jpg", "/api/subjects/biologia", "api/users/laura-mendez/photo"],
)
def test_a_photo_url_outside_the_users_api_is_refused(photo_url: str) -> None:
    _refused(CREATE_RESPONSE, {**USER_BODY, "photo_url": photo_url})


@pytest.mark.parametrize("user_id", ["laura-mendez", "laura-mendez-2", "L2"])
def test_a_user_id_follows_the_pattern_of_the_other_ids(user_id: str) -> None:
    _accepted(CREATE_RESPONSE, {**USER_BODY, "id": user_id})


@pytest.mark.parametrize("user_id", ["Laura Méndez", "-laura", "laura.mendez", ""])
def test_a_user_id_that_is_not_a_slug_is_refused(user_id: str) -> None:
    _refused(CREATE_RESPONSE, {**USER_BODY, "id": user_id})


def test_the_active_user_constants() -> None:
    assert USER_HEADER == "X-SA-User"
    assert USER_COOKIE == "sa_user"
    assert USER_PHOTO_CONTENT_TYPES == ("image/jpeg", "image/png", "image/webp")


def test_the_two_user_error_codes_are_1_8() -> None:
    assert ErrorCode.USER_REQUIRED == "user_required"
    assert ErrorCode.USER_NOT_FOUND == "user_not_found"
    assert USER_ERROR_CODES_SINCE == (1, 8)
    assert ERROR_CODES_SINCE[ErrorCode.USER_REQUIRED] == USER_ERROR_CODES_SINCE
    assert ERROR_CODES_SINCE[ErrorCode.USER_NOT_FOUND] == USER_ERROR_CODES_SINCE


def test_every_error_code_has_a_since_and_the_older_ones_keep_theirs() -> None:
    added_in_1_8 = {ErrorCode.USER_REQUIRED, ErrorCode.USER_NOT_FOUND}
    assert set(ERROR_CODES_SINCE) == set(ErrorCode)
    assert all(
        since == (1, 2) for code, since in ERROR_CODES_SINCE.items() if code not in added_in_1_8
    )
