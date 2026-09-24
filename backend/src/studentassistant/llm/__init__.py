"""Claude client wrapper: model roles, caching, structured outputs, cost ledger, fakes."""

from studentassistant.llm.caching import cache_stable_prefix, cached_block, system_blocks
from studentassistant.llm.client import LLMClient, backoff_delay, get_client
from studentassistant.llm.errors import (
    FakeClaudeExhaustedError,
    LLMAPIError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMRetriesExhaustedError,
    LLMServerError,
    LLMTransientError,
    PromptNotFoundError,
    RefusalError,
    StructuredOutputError,
    UnknownRoleError,
)
from studentassistant.llm.fake import FakeClaude, no_sleep
from studentassistant.llm.prompts import Prompt, PromptRegistry, content_hash, load_prompt
from studentassistant.llm.structured import StructuredResult, strict_tool, structured
from studentassistant.llm.transport import AnthropicTransport, Transport
from studentassistant.llm.types import ROLES, LLMRequest, LLMResponse, Role, ToolCall, Usage

__all__ = [
    "ROLES",
    "AnthropicTransport",
    "FakeClaude",
    "FakeClaudeExhaustedError",
    "LLMAPIError",
    "LLMClient",
    "LLMConnectionError",
    "LLMError",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMResponse",
    "LLMRetriesExhaustedError",
    "LLMServerError",
    "LLMTransientError",
    "Prompt",
    "PromptNotFoundError",
    "PromptRegistry",
    "RefusalError",
    "Role",
    "StructuredOutputError",
    "StructuredResult",
    "ToolCall",
    "Transport",
    "UnknownRoleError",
    "Usage",
    "backoff_delay",
    "cache_stable_prefix",
    "cached_block",
    "content_hash",
    "get_client",
    "load_prompt",
    "no_sleep",
    "strict_tool",
    "structured",
    "system_blocks",
]
