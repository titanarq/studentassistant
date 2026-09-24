"""Whether an Anthropic API key works: one free call (`GET /v1/models?limit=1`), no tokens spent.

`studentassistant doctor --api-call` uses it. Listing models is authenticated like any request
but costs nothing, so it proves the key without writing a ledger entry or touching a cost cap.
"""

from __future__ import annotations

from typing import Any

import anthropic

from studentassistant.llm.transport import map_sdk_error

DEFAULT_KEY_CHECK_TIMEOUT_SECONDS = 15.0


def check_api_key(
    api_key: str | None = None,
    *,
    timeout: float = DEFAULT_KEY_CHECK_TIMEOUT_SECONDS,
    sdk: Any = None,
) -> None:
    """Make one free authenticated call; return when the key works.

    `api_key` unset means the SDK's own lookup (`ANTHROPIC_API_KEY`); `sdk` replaces the
    `anthropic.Anthropic` client (tests). No retry: a failure is reported at once.

    Raises:
        LLMAPIError: the key is refused (401/403) or the request is rejected otherwise.
        LLMTransientError: offline, timed out, rate-limited or a server error.
        LLMError: no key could be found at all.
    """
    try:
        client = sdk or anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout)
        client.models.list(limit=1)
    except Exception as error:  # every SDK failure becomes one of ours
        raise map_sdk_error(error) from error
