"""`LLMClient` per role (ADR-0004): model, effort and max_tokens from `[llm.roles.<role>]`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from studentassistant.config import Effort, LlmRoleSettings, LlmSettings, Settings
from studentassistant.llm.caching import cache_stable_prefix, system_blocks
from studentassistant.llm.cost import Clock, LedgerBinding, check_caps, record_call, utc_now
from studentassistant.llm.errors import (
    LLMRetriesExhaustedError,
    LLMTransientError,
    UnknownRoleError,
)
from studentassistant.llm.transport import AnthropicTransport, TextSink, Transport
from studentassistant.llm.types import ROLES, LLMRequest, LLMResponse

Sleep = Callable[[float], Awaitable[None]]

logger = logging.getLogger(__name__)

# Exponential backoff between attempts: 1 s, 2 s, 4 s... capped; a server `retry-after` wins.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0

# Opus 5.5 rejects a forced tool choice; ADR-0004 forbids it for every role.
_ALLOWED_TOOL_CHOICES = {"auto", "none"}


def backoff_delay(attempt: int, error: LLMTransientError) -> float:
    """Seconds to wait after failed attempt number `attempt` (1-based)."""
    if error.retry_after is not None:
        return min(max(error.retry_after, 0.0), BACKOFF_MAX_SECONDS)
    return min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)


class LLMClient:
    """A Claude client bound to one role. Every call streams and retries transient failures.

    With a `ledger` binding, each call is checked against the cost caps of `llm_settings` first
    and every successful one is appended to the bound topic's ledger; without one, neither.
    """

    def __init__(
        self,
        role: str,
        settings: LlmRoleSettings,
        *,
        transport: Transport,
        max_attempts: int,
        sleep: Sleep = asyncio.sleep,
        ledger: LedgerBinding | None = None,
        llm_settings: LlmSettings | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.role = role
        self.settings = settings
        self.transport = transport
        self.max_attempts = max_attempts
        self.ledger = ledger
        self.llm_settings = llm_settings or LlmSettings()
        self._sleep = sleep
        self._clock = clock

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def effort(self) -> Effort:
        return self.settings.effort

    def build_request(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | list[str] | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        cache: bool = True,
        prompt_hash: str | None = None,
    ) -> LLMRequest:
        """The request `create` sends; with `cache`, the stable prefix carries the breakpoint."""
        if tool_choice is not None and tool_choice.get("type") not in _ALLOWED_TOOL_CHOICES:
            raise ValueError(
                f"tool_choice {tool_choice.get('type')!r} is not allowed: use 'auto' plus an "
                "instruction (forced tool use is rejected by Opus 5.5, ADR-0004)"
            )
        blocks = system_blocks(system)
        tool_list = list(tools or [])
        if cache:
            blocks, tool_list = cache_stable_prefix(blocks, tool_list)
        return LLMRequest(
            model=self.settings.model,
            max_tokens=max_tokens or self.settings.max_tokens,
            effort=self.settings.effort,
            system=blocks,
            messages=messages,
            tools=tool_list,
            tool_choice=tool_choice,
            role=self.role,
            prompt_hash=prompt_hash,
        )

    async def send(self, request: LLMRequest, on_text: TextSink | None = None) -> LLMResponse:
        """Send a built request, retrying 429/5xx/connection errors up to `max_attempts` times.

        With `on_text`, the answer's text deltas are passed to it as they stream in; after a retry
        the deltas start again from the beginning of the new answer.
        """
        for attempt in range(1, self.max_attempts + 1):
            try:
                if on_text is None:
                    return await self.transport.send(request)
                return await self.transport.send(request, on_text=on_text)  # type: ignore[call-arg]
            except LLMTransientError as error:
                if attempt == self.max_attempts:
                    raise LLMRetriesExhaustedError(attempt, error) from error
                await self._sleep(backoff_delay(attempt, error))
        raise AssertionError("unreachable")  # pragma: no cover

    async def create(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | list[str] | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        cache: bool = True,
        prompt_hash: str | None = None,
        confirm_over_cap: bool = False,
        on_text: TextSink | None = None,
    ) -> LLMResponse:
        """One Claude call: build the request for this role and send it.

        `on_text` receives the text deltas of the answer as they stream in (for a live reply); the
        returned `LLMResponse` is still the whole final message.

        With a ledger binding: raises `CostCapReachedError` (observer, transcriber) or
        `CostConfirmationRequiredError` (editor, generator, unless `confirm_over_cap`) when a cost
        cap is reached, and records the call once it succeeds.
        """
        if self.ledger is not None:
            check_caps(
                self.role,
                self.ledger,
                self.llm_settings,
                now=self._clock(),
                confirm_over_cap=confirm_over_cap,
            )
        request = self.build_request(
            messages,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            cache=cache,
            prompt_hash=prompt_hash,
        )
        response = await self.send(request, on_text)
        if self.ledger is not None:
            self._record(request, response)
        return response

    def _record(self, request: LLMRequest, response: LLMResponse) -> None:
        # The call is done and paid for: a ledger that cannot be written must not lose the answer.
        assert self.ledger is not None
        try:
            record_call(self.ledger, request, response, self.llm_settings.prices, now=self._clock())
        except Exception:
            logger.exception(
                "could not record a %s call in the ledger of %s/%s",
                self.role,
                self.ledger.subject,
                self.ledger.topic,
            )


def get_client(
    role: str,
    *,
    settings: Settings | None = None,
    transport: Transport | None = None,
    sleep: Sleep = asyncio.sleep,
    ledger: LedgerBinding | None = None,
    clock: Clock = utc_now,
) -> LLMClient:
    """The client for `role`, configured from `[llm.roles.<role>]`.

    Tests pass `transport=FakeClaude(...)`; without one the real, streaming Anthropic transport is
    used. With `ledger`, calls are capped and recorded (see `LLMClient`); `clock` gives the UTC
    time of each entry and of the day cap. Raises `UnknownRoleError` for a role outside `ROLES`.
    """
    if role not in ROLES:
        raise UnknownRoleError(role, ROLES)
    settings = settings or Settings()
    role_settings: LlmRoleSettings = getattr(settings.llm.roles, role)
    return LLMClient(
        role,
        role_settings,
        transport=transport or AnthropicTransport(),
        max_attempts=settings.llm.max_attempts,
        sleep=sleep,
        ledger=ledger,
        llm_settings=settings.llm,
        clock=clock,
    )
