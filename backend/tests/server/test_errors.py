"""`server.errors`: coded REST error bodies, shaped to the caller's protocol version."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from studentassistant.llm import CostConfirmationRequiredError
from studentassistant.protocol import PROTOCOL_VERSION, ErrorCode
from studentassistant.server.auth import Principal
from studentassistant.server.errors import (
    USER_REQUIRED_DETAIL,
    ApiError,
    cost_cap_error,
    error_body,
    install_error_handler,
    speaks_error_codes,
    speaks_this_error_code,
    unknown_user_error,
    user_required_error,
)


@pytest.mark.parametrize(
    ("version", "speaks"),
    [("1.0", False), ("1.1", False), ("1.2", True), ("1.9", True), (PROTOCOL_VERSION, True)],
)
def test_codes_are_sent_from_1_2_on(version: str, speaks: bool) -> None:
    assert speaks_error_codes(version) is speaks


@pytest.mark.parametrize(
    ("version", "speaks"),
    [("1.0", False), ("1.7", False), ("1.8", True), ("1.9", True), (PROTOCOL_VERSION, True)],
)
def test_the_user_codes_are_sent_from_1_8_on(version: str, speaks: bool) -> None:
    assert speaks_this_error_code(version, ErrorCode.USER_REQUIRED) is speaks
    assert speaks_this_error_code(version, ErrorCode.USER_NOT_FOUND) is speaks


def test_a_malformed_or_foreign_version_gets_no_code() -> None:
    assert speaks_error_codes("banana") is False
    assert speaks_error_codes("2.0") is False
    assert speaks_this_error_code("banana", ErrorCode.USER_REQUIRED) is False
    assert speaks_this_error_code("2.0", ErrorCode.USER_NOT_FOUND) is False


def test_the_cost_cap_error_keeps_its_spanish_sentence() -> None:
    error = CostConfirmationRequiredError("over", cap="day", total_usd=5.1, limit_usd=5.0)
    refused = cost_cap_error(error, "Confirma para continuar igualmente.")
    assert (refused.status_code, refused.code) == (409, ErrorCode.COST_CAP_REACHED)
    assert refused.detail == (
        "Se ha alcanzado el límite de gasto del día (5.10 de 5.00 USD)."
        " Confirma para continuar igualmente."
    )


def test_the_handler_answers_the_code_and_leaves_plain_errors_alone() -> None:
    app = FastAPI()
    install_error_handler(app)

    @app.get("/coded")
    def coded() -> None:
        raise ApiError(409, "Esa duda ya está cerrada.", ErrorCode.DOUBT_CLOSED, {"X-A": "b"})

    @app.get("/plain")
    def plain() -> None:
        raise HTTPException(404, "No existe.")

    with TestClient(app) as client:
        response = client.get("/coded")  # no principal: this build's own version
        assert response.status_code == 409 and response.headers["X-A"] == "b"
        assert response.json() == {"detail": "Esa duda ya está cerrada.", "code": "doubt_closed"}
        assert client.get("/plain").json() == {"detail": "No existe."}


def test_the_two_user_refusals_are_coded_and_in_spanish() -> None:
    required = user_required_error()
    assert (required.status_code, required.code) == (400, ErrorCode.USER_REQUIRED)
    assert required.detail == USER_REQUIRED_DETAIL
    unknown = unknown_user_error("lucia-fernandez")
    assert (unknown.status_code, unknown.code) == (404, ErrorCode.USER_NOT_FOUND)
    assert unknown.detail == "El usuario «lucia-fernandez» no existe en esta bóveda."


def paired_caller(protocol_version: str) -> Request:
    """A request whose caller the middleware left as a device that paired as `protocol_version`."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/subjects",
            "headers": [],
            "state": {"principal": Principal(device_id="dev-1", protocol_version=protocol_version)},
        }
    )


def test_a_code_travels_only_to_a_version_that_has_it() -> None:
    """A device paired as 1.4 branches on `doubt_closed` and reads a user refusal by its status."""
    paired_1_4 = paired_caller("1.4")
    closed = ApiError(409, "Esa duda ya está cerrada.", ErrorCode.DOUBT_CLOSED)
    assert error_body(paired_1_4, closed) == {
        "detail": "Esa duda ya está cerrada.",
        "code": "doubt_closed",
    }
    assert error_body(paired_1_4, user_required_error()) == {"detail": USER_REQUIRED_DETAIL}
    assert error_body(paired_caller("1.8"), user_required_error()) == {
        "detail": USER_REQUIRED_DETAIL,
        "code": "user_required",
    }


def test_the_handler_answers_the_user_refusals_with_their_status() -> None:
    app = FastAPI()
    install_error_handler(app)

    @app.get("/required")
    def required() -> None:
        raise user_required_error()

    @app.get("/unknown")
    def unknown() -> None:
        raise unknown_user_error("nadie")

    with TestClient(app) as client:  # no principal: the PC itself, which speaks this build
        missing_user = client.get("/required")
        assert missing_user.status_code == 400
        assert missing_user.json() == {"detail": USER_REQUIRED_DETAIL, "code": "user_required"}
        unknown_user = client.get("/unknown")
        assert unknown_user.status_code == 404
        assert unknown_user.json() == {
            "detail": "El usuario «nadie» no existe en esta bóveda.",
            "code": "user_not_found",
        }
