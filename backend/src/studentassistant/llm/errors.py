"""Typed errors of `studentassistant.llm`: no raw `anthropic` exception ever leaves the module."""

from __future__ import annotations


class LLMError(Exception):
    """Base class of every error the llm module raises."""


class UnknownRoleError(LLMError, ValueError):
    """A role other than `observer`, `transcriber`, `editor` or `generator` was asked for."""

    def __init__(self, role: str, known: tuple[str, ...]) -> None:
        super().__init__(f"unknown Claude role {role!r}; known roles: {', '.join(known)}")
        self.role = role
        self.known = known


class LLMAPIError(LLMError):
    """The API answered with an error status that retrying does not fix (400, 401, 403, 404...)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMTransientError(LLMError):
    """A failure worth retrying with backoff: rate limit, server error or lost connection."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        # Seconds the server asked us to wait (`retry-after`), when it said.
        self.retry_after = retry_after


class LLMRateLimitError(LLMTransientError):
    """HTTP 429."""


class LLMServerError(LLMTransientError):
    """HTTP 5xx (529 overloaded included)."""

    def __init__(
        self, message: str, *, status_code: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message, retry_after=retry_after)
        self.status_code = status_code


class LLMConnectionError(LLMTransientError):
    """The request never got an answer: connection refused, reset, DNS or timeout."""


class LLMRetriesExhaustedError(LLMError):
    """Every allowed attempt failed with a transient error; `last_error` is the final one."""

    def __init__(self, attempts: int, last_error: LLMTransientError) -> None:
        super().__init__(f"Claude call failed after {attempts} attempts: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


class StructuredOutputError(LLMError):
    """Claude did not produce a valid call of the structured-output tool, even after one re-ask."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"no valid {tool_name!r} tool call after one re-ask: {reason}")
        self.tool_name = tool_name
        self.reason = reason


class RefusalError(LLMError):
    """Claude declined the request (`stop_reason: refusal`)."""


class PromptNotFoundError(LLMError, KeyError):
    """No `prompts/<name>.md` file exists."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "prompt not found"


class FakeClaudeExhaustedError(LLMError, AssertionError):
    """A test's `FakeClaude` received more requests than it had scripted replies."""
