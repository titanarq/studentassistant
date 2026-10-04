"""REST error bodies with a machine-readable `code` (protocol 1.2, `protocol/README.md`).

Every REST error body is `{"detail": "<Spanish sentence>"}`. The refusals a client branches on
raise `ApiError` instead of a bare `HTTPException`: the same status and `detail`, plus an
`ErrorCode` that `api_error_handler` (installed by `install_error_handler`) adds as `code` when
the caller's negotiated protocol version has that very code (`protocol.ERROR_CODES_SINCE`) -- a
device that paired as a 1.0/1.1 client gets the plain `{"detail": ...}` body, and one that paired
before 1.8 gets no `user_required` / `user_not_found` either, as the version rules want. Routes use
the helpers below; a new route that needs a coded refusal adds its code to
`studentassistant.protocol.ErrorCode` and raises `ApiError` too.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from studentassistant.llm import CostConfirmationRequiredError
from studentassistant.protocol import (
    ERROR_CODE_SINCE,
    ERROR_CODES_SINCE,
    PROTOCOL_VERSION,
    ErrorCode,
    negotiate,
    parse_version,
)

USER_REQUIRED_DETAIL = "No se sabe para qué usuario es esta petición: vuelve a elegir quién eres."
"""The 400 `user_required` sentence: what is missing, and the one thing the student can do."""


class ApiError(HTTPException):
    """An `HTTPException` whose body also carries `code` for a client that speaks it."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        code: ErrorCode,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=dict(headers or {}))
        self.code = code


def cost_cap_error(error: CostConfirmationRequiredError, then: str) -> ApiError:
    """409 `cost_cap_reached` for a reached cap; `then` ends the sentence ("Confirma para ...")."""
    scope = "de la sesión" if error.cap == "session" else "del día"
    detail = (
        f"Se ha alcanzado el límite de gasto {scope} ({error.total_usd:.2f} de"
        f" {error.limit_usd:.2f} USD). {then}"
    )
    return ApiError(409, detail, ErrorCode.COST_CAP_REACHED)


def user_required_error() -> ApiError:
    """400 `user_required`: the request names no user and this vault holds more than one (#549).

    The student's own client shows the selection screen again; the sentence says what is missing
    and what to do about it, because a web page that lost its `sa_user` cookie has nothing else to
    go by.
    """
    return ApiError(400, USER_REQUIRED_DETAIL, ErrorCode.USER_REQUIRED)


def unknown_user_error(user_id: str) -> ApiError:
    """404 `user_not_found`: this vault has no user `user_id` (#549).

    The id is in the sentence because it is the one thing that says which selection went stale --
    a user another PC removed, or a cookie of a vault this backend no longer serves.
    """
    return ApiError(
        404, f"El usuario «{user_id}» no existe en esta bóveda.", ErrorCode.USER_NOT_FOUND
    )


def speaks_error_codes(protocol_version: str) -> bool:
    """Whether a client that paired with `protocol_version` gets `code` in error bodies."""
    try:
        return parse_version(negotiate(protocol_version)) >= ERROR_CODE_SINCE
    except ValueError:
        return False


def speaks_this_error_code(protocol_version: str, code: ErrorCode) -> bool:
    """Whether a client that paired with `protocol_version` gets `code` itself.

    `speaks_error_codes` asks whether a caller gets codes at all; this asks about one of them,
    because a code travels only to a version that has it (`protocol.ERROR_CODES_SINCE`). The two
    user codes are new in 1.8, so a device paired as 1.2-1.7 gets the Spanish `detail` and reads
    the status alone, which is what a client that has never heard of a code does anyway.
    """
    try:
        return parse_version(negotiate(protocol_version)) >= ERROR_CODES_SINCE[code]
    except ValueError:
        return False


def caller_speaks_error_codes(request: Request) -> bool:
    """Whether this request's caller gets `code` (the PC itself speaks this build's version)."""
    principal = getattr(request.state, "principal", None)
    return speaks_error_codes(getattr(principal, "protocol_version", PROTOCOL_VERSION))


def caller_speaks_this_error_code(request: Request, code: ErrorCode) -> bool:
    """Whether this request's caller gets `code` itself (see `speaks_this_error_code`)."""
    principal = getattr(request.state, "principal", None)
    return speaks_this_error_code(getattr(principal, "protocol_version", PROTOCOL_VERSION), code)


def error_body(request: Request, error: ApiError) -> dict[str, Any]:
    """`{"detail", "code"?}` shaped for the caller."""
    body: dict[str, Any] = {"detail": error.detail}
    if caller_speaks_this_error_code(request, error.code):
        body["code"] = error.code.value
    return body


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code, content=error_body(request, exc), headers=exc.headers
    )


def install_error_handler(app: FastAPI) -> None:
    """Answer every `ApiError` with its coded body (other `HTTPException`s stay as they are)."""
    app.add_exception_handler(ApiError, api_error_handler)
