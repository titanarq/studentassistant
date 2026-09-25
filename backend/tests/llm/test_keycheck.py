"""`check_api_key`: one free `GET /v1/models?limit=1`; SDK errors become ours. No network."""

from __future__ import annotations

import anthropic
import httpx2
import pytest

from studentassistant.llm import LLMAPIError, LLMConnectionError, check_api_key


def sdk_answering(handler) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key="k",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def test_a_working_key_lists_one_model() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"data": [], "has_more": False})

    check_api_key(sdk=sdk_answering(handler))

    assert [(r.method, r.url.path, r.url.params.get("limit")) for r in seen] == [
        ("GET", "/v1/models", "1")
    ]


def test_a_refused_key_is_an_api_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = {"type": "error", "error": {"type": "authentication_error", "message": "bad"}}
        return httpx2.Response(401, json=body)

    with pytest.raises(LLMAPIError) as caught:
        check_api_key(sdk=sdk_answering(handler))
    assert caught.value.status_code == 401


def test_offline_is_a_connection_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("offline", request=request)

    with pytest.raises(LLMConnectionError):
        check_api_key(sdk=sdk_answering(handler))
