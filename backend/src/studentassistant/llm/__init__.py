"""Claude client wrapper: model roles, caching, structured outputs, cost ledger, fakes."""

from studentassistant.llm.caching import cache_stable_prefix, cached_block, system_blocks
from studentassistant.llm.client import LLMClient, backoff_delay, get_client
from studentassistant.llm.cost import CostStatus, LedgerBinding, cost_status, estimate_usd
from studentassistant.llm.credentials import find_ant_profile
from studentassistant.llm.errors import (
    CostCapError,
    CostCapReachedError,
    CostConfirmationRequiredError,
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
from studentassistant.llm.keycheck import check_api_key
from studentassistant.llm.prompts import Prompt, PromptRegistry, content_hash, load_prompt
from studentassistant.llm.structured import StructuredResult, strict_tool, structured
from studentassistant.llm.transport import AnthropicTransport, Transport
from studentassistant.llm.types import ROLES, LLMRequest, LLMResponse, Role, ToolCall, Usage

__all__ = [
    "ROLES",
    "AnthropicTransport",
    "CostCapError",
    "CostCapReachedError",
    "CostConfirmationRequiredError",
    "CostStatus",
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
    "LedgerBinding",
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
    "check_api_key",
    "content_hash",
    "cost_status",
    "estimate_usd",
    "find_ant_profile",
    "get_client",
    "load_prompt",
    "no_sleep",
    "strict_tool",
    "structured",
    "system_blocks",
]
