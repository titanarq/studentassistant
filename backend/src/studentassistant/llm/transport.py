"""The one place that talks to the Anthropic SDK: streams a request, maps SDK errors to ours."""

from __future__ import annotations

from typing import Any, Protocol

import anthropic

from studentassistant.llm.errors import (
    LLMAPIError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
)
from studentassistant.llm.types import LLMRequest, LLMResponse, Usage


class Transport(Protocol):
    """Sends one request and returns the final message. `FakeClaude` is the test implementation."""

    async def send(self, request: LLMRequest) -> LLMResponse: ...


def _retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", None) or {}
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            return float(value) / scale
        except ValueError:
            continue
    return None


# The status an `error` event received mid-stream (on an HTTP 200 response) stands for.
_STREAM_ERROR_STATUS = {"rate_limit_error": 429, "overloaded_error": 529, "api_error": 500}


def _effective_status(error: anthropic.APIStatusError) -> int:
    if error.status_code >= 400:
        return error.status_code
    body = error.body
    kind = body.get("error", {}).get("type") if isinstance(body, dict) else None
    return _STREAM_ERROR_STATUS.get(kind or "", 400)


def map_sdk_error(error: Exception) -> LLMError:
    """The llm-module error for an `anthropic` exception (or any unexpected one)."""
    if isinstance(error, anthropic.APIConnectionError):  # APITimeoutError included
        return LLMConnectionError(str(error) or type(error).__name__)
    if isinstance(error, anthropic.APIStatusError):
        status = _effective_status(error)
        retry_after = _retry_after(error.response)
        if status == 429:
            return LLMRateLimitError(error.message, retry_after=retry_after)
        if status >= 500:
            return LLMServerError(error.message, status_code=status, retry_after=retry_after)
        return LLMAPIError(error.message, status_code=status)
    if isinstance(error, anthropic.AnthropicError):
        return LLMAPIError(str(error))
    return LLMError(f"{type(error).__name__}: {error}")


def response_from_message(message: Any) -> LLMResponse:
    """Our `LLMResponse` from an SDK `Message`."""
    usage = message.usage
    return LLMResponse(
        model=message.model,
        stop_reason=message.stop_reason,
        content=[block.model_dump(mode="json", exclude_none=True) for block in message.content],
        usage=Usage(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        ),
    )


class AnthropicTransport:
    """The real transport: always streams (`messages.stream` + `get_final_message`).

    The SDK's own retries are off (`max_retries=0`); `LLMClient` retries with its own bounded,
    injectable backoff. The SDK client is created on first use, so building an `LLMClient` needs
    no API key; the key comes from the machine (`ANTHROPIC_API_KEY` or an `ant auth` profile).
    """

    def __init__(self, sdk_client: anthropic.AsyncAnthropic | None = None) -> None:
        self._sdk = sdk_client

    def _client(self) -> anthropic.AsyncAnthropic:
        if self._sdk is None:
            self._sdk = anthropic.AsyncAnthropic(max_retries=0)
        return self._sdk

    async def send(self, request: LLMRequest) -> LLMResponse:
        try:
            client = self._client().with_options(max_retries=0)
            async with client.messages.stream(**request.api_params()) as stream:
                message = await stream.get_final_message()
        except Exception as error:  # every SDK failure becomes one of ours
            raise map_sdk_error(error) from error
        return response_from_message(message)
