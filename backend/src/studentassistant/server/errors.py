"""REST error bodies with a machine-readable `code` (protocol 1.2, `protocol/README.md`).

Every REST error body is `{"detail": "<Spanish sentence>"}`. The refusals a client branches on
raise `ApiError` instead of a bare `HTTPException`: the same status and `detail`, plus an
`ErrorCode` that `api_error_handler` (installed by `install_error_handler`) adds as `code` when
the caller's negotiated protocol version has it -- a device that paired as a 1.0/1.1 client gets
the plain `{"detail": ...}` body, as the version rules want. Routes use the helpers below; a new
route that needs a coded refusal adds its code to `studentassistant.protocol.ErrorCode` and raises
`ApiError` too.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from studentassistant.llm import CostConfirmationRequiredError
from studentassistant.protocol import (
    ERROR_CODE_SINCE,
    PROTOCOL_VERSION,
    ErrorCode,
    negotiate,
    parse_version,
)


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


def speaks_error_codes(protocol_version: str) -> bool:
    """Whether a client that paired with `protocol_version` gets `code` in error bodies."""
    try:
        return parse_version(negotiate(protocol_version)) >= ERROR_CODE_SINCE
    except ValueError:
        return False


def caller_speaks_error_codes(request: Request) -> bool:
    """Whether this request's caller gets `code` (the PC itself speaks this build's version)."""
    principal = getattr(request.state, "principal", None)
    return speaks_error_codes(getattr(principal, "protocol_version", PROTOCOL_VERSION))


def error_body(request: Request, error: ApiError) -> dict[str, Any]:
    """`{"detail", "code"?}` shaped for the caller."""
    body: dict[str, Any] = {"detail": error.detail}
    if caller_speaks_error_codes(request):
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
