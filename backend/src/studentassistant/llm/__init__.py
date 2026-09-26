"""Claude client wrapper: model roles, caching, structured outputs, cost ledger, fakes."""

from studentassistant.llm.backend import (
    ResolvedBackend,
    api_key_available,
    default_transport,
    resolve_backend,
)
from studentassistant.llm.caching import cache_stable_prefix, cached_block, system_blocks
from studentassistant.llm.claude_code import (
    ClaudeCodeStatus,
    ClaudeCodeTransport,
    check_claude_code,
)
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
from studentassistant.llm.fake import FakeClaude, no_sleep, web_fetch_blocks, web_search_blocks
from studentassistant.llm.keycheck import check_api_key
from studentassistant.llm.prompts import Prompt, PromptRegistry, content_hash, load_prompt
from studentassistant.llm.structured import StructuredResult, strict_tool, structured
from studentassistant.llm.transport import AnthropicTransport, TextSink, Transport
from studentassistant.llm.types import (
    ROLES,
    Billing,
    LLMRequest,
    LLMResponse,
    Role,
    ToolCall,
    Usage,
)
from studentassistant.llm.web import (
    FetchedDocument,
    ServerToolRun,
    WebSearchHit,
    WebToolResults,
    parse_web_results,
    run_server_tools,
    web_fetch_tool,
    web_search_tool,
)

__all__ = [
    "AnthropicTransport",
    "Billing",
    "ClaudeCodeStatus",
    "ClaudeCodeTransport",
    "CostCapError",
    "CostCapReachedError",
    "CostConfirmationRequiredError",
    "CostStatus",
    "FakeClaude",
    "FakeClaudeExhaustedError",
    "FetchedDocument",
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
    "ROLES",
    "RefusalError",
    "ResolvedBackend",
    "Role",
    "ServerToolRun",
    "StructuredOutputError",
    "StructuredResult",
    "TextSink",
    "ToolCall",
    "Transport",
    "UnknownRoleError",
    "Usage",
    "WebSearchHit",
    "WebToolResults",
    "api_key_available",
    "backoff_delay",
    "cache_stable_prefix",
    "cached_block",
    "check_api_key",
    "check_claude_code",
    "content_hash",
    "cost_status",
    "default_transport",
    "estimate_usd",
    "find_ant_profile",
    "get_client",
    "load_prompt",
    "no_sleep",
    "parse_web_results",
    "resolve_backend",
    "run_server_tools",
    "strict_tool",
    "structured",
    "system_blocks",
    "web_fetch_blocks",
    "web_fetch_tool",
    "web_search_blocks",
    "web_search_tool",
]
