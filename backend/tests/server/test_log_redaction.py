"""Neither a bearer token nor a pairing code ever reaches a log record."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studentassistant.server.redaction import REDACTED, install_log_redaction, redact

PairBody = Callable[[str], dict[str, Any]]


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    formatter = logging.Formatter("%(name)s %(message)s")
    return "\n".join(formatter.format(record) for record in caplog.records)


def test_an_authenticated_request_and_a_failed_pairing_log_no_secret(
    caplog: pytest.LogCaptureFixture,
    local: TestClient,
    lan: TestClient,
    pair_body: PairBody,
) -> None:
    caplog.set_level(logging.DEBUG)
    code = local.post("/api/pair/codes").json()["code"]
    token = lan.post("/api/pair", json=pair_body(code)).json()["token"]
    wrong_code = local.post("/api/pair/codes").json()["code"]

    authenticated = lan.get("/api/health", headers={"Authorization": f"Bearer {token}"})
    refused = lan.post("/api/pair", json=pair_body(code))  # already redeemed
    # What uvicorn's access log writes for a WebSocket that carries its token in the query.
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "192.168.1.30:50000", "GET", f"/ws?token={token}", "1.1", 101
    )
    logging.getLogger("uvicorn.error").info("pairing body %s", pair_body(wrong_code))

    assert authenticated.status_code == 200
    assert refused.status_code == 401
    logged = _everything_logged(caplog)
    assert "pairing refused" in logged  # the failed pairing was logged...
    assert "/ws?token=" in logged  # ...and so was the access line
    for secret in (token, code, wrong_code):
        assert secret not in logged


def test_a_traceback_is_redacted_too(caplog: pytest.LogCaptureFixture) -> None:
    install_log_redaction()
    token = "sa_" + "x" * 43
    try:
        raise ValueError(f"bad header Authorization: Bearer {token}")
    except ValueError:
        logging.getLogger("studentassistant.test").exception("request failed")

    logged = caplog.text + _everything_logged(caplog)
    assert "request failed" in logged
    assert token not in logged
    assert REDACTED in logged


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ("GET /ws/session?token=s3cr3t&x=1", "s3cr3t"),
        ('{"pairing_code": "k7q2-9xma"}', "k7q2-9xma"),
        ('{"token": "anything"}', "anything"),
        ("token sa_AbCdEfGhIjKlMnOpQrStUv here", "sa_AbCdEfGhIjKlMnOpQrStUv"),
        ("code K7Q2-9XMA shown", "K7Q2-9XMA"),
    ],
)
def test_redact_removes_each_shape_of_secret(text: str, secret: str) -> None:
    assert secret not in redact(text)
    assert REDACTED in redact(text)


def test_redact_leaves_ordinary_text_alone() -> None:
    text = 'GET /api/health HTTP/1.1" 200'

    assert redact(text) == text
