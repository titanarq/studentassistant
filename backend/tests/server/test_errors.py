"""`server.errors`: coded REST error bodies, shaped to the caller's protocol version."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from studentassistant.llm import CostConfirmationRequiredError
from studentassistant.protocol import PROTOCOL_VERSION, ErrorCode
from studentassistant.server.errors import (
    ApiError,
    cost_cap_error,
    install_error_handler,
    speaks_error_codes,
)


@pytest.mark.parametrize(
    ("version", "speaks"),
    [("1.0", False), ("1.1", False), ("1.2", True), ("1.9", True), (PROTOCOL_VERSION, True)],
)
def test_codes_are_sent_from_1_2_on(version: str, speaks: bool) -> None:
    assert speaks_error_codes(version) is speaks


def test_a_malformed_or_foreign_version_gets_no_code() -> None:
    assert speaks_error_codes("banana") is False
    assert speaks_error_codes("2.0") is False


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
