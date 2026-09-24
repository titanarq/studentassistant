# Module: llm

**Lives in:** `backend/src/studentassistant/llm/` and `backend/src/studentassistant/prompts/`.
Decision: ADR-0004.

## Responsibility
- `LLMClient` per role from config; streaming; retries on 429/5xx; prompt caching helpers;
  strict-tool structured outputs validated against Pydantic models.
- Prompt registry: loads versioned prompt files, exposes their content hash.
- Cost ledger entries and cost caps.
- `FakeClaude`: scripted responses (including tool calls and usage) for every other module's tests.

## Boundaries
- The only importer of `anthropic`. No domain logic.

## Configuration

`[llm.roles.<role>]` in `config.toml` (or `SA_LLM__ROLES__<ROLE>__<KEY>`), one table per role
`observer`, `transcriber`, `editor`, `generator`. A partial table keeps that role's defaults.

| key | observer / transcriber | editor / generator |
|---|---|---|
| `model` | `claude-sonnet-5` | `claude-opus-5-5` |
| `effort` (`low`..`max`, always sent) | `medium` | `high` |
| `max_tokens` | `16000` | `64000` |

`[llm] max_attempts = 4`: attempts per call (first one included) before a 429/5xx/connection error
surfaces. The API key comes from the machine (`ANTHROPIC_API_KEY` or an `ant auth` profile).

## Public surface (`from studentassistant.llm import ...`)

- `get_client(role, *, settings=None, transport=None, sleep=asyncio.sleep) -> LLMClient`;
  `UnknownRoleError` for any other role. `LLMClient.create(messages, *, system=None, tools=None,
  tool_choice=None, max_tokens=None, cache=True, prompt_hash=None) -> LLMResponse` (async).
  Every call streams (`messages.stream` + `get_final_message`), sends `output_config.effort`,
  never sends `thinking`, and refuses a forced `tool_choice` (`any`/`tool`) with `ValueError`.
  `build_request(...)` returns the `LLMRequest` without sending it.
- `LLMResponse`: `content` (raw blocks; `assistant_turn()` to append it back), `text`,
  `tool_calls` (`ToolCall.input_json`, `parsed_input()`), `stop_reason`, `usage`
  (input/output/cache-creation/cache-read tokens), `model`.
- Caching: with `cache=True` (default) the last system block (or, with no system, the last
  tool) gets `cache_control: ephemeral`, so tools + system form the cached prefix and the messages
  stay volatile. `cached_block(text)` marks an extra breakpoint (e.g. topic sources);
  `cache_stable_prefix`, `system_blocks` are the underlying helpers.
- `structured(client, messages, OutputModel, *, tool_name, tool_description, system=None,
  max_tokens=None, prompt_hash=None) -> StructuredResult` (`.value`, `.responses`): a strict tool
  (`strict_tool`) with `tool_choice: auto` and the `structured-output` prompt as instruction;
  the input is parsed with `json` and validated with Pydantic; one re-ask carrying the error,
  then `StructuredOutputError`. `RefusalError` on `stop_reason: refusal`.
- Prompts: `load_prompt(name) -> Prompt` (`name`, `content`, `hash` = `sha256:<hex>`, `render`)
  for `backend/src/studentassistant/prompts/<name>.md`; `PromptRegistry(directory)` (`names`,
  `get`); `content_hash`. Missing prompt: `PromptNotFoundError`.
- Errors (all subclass `LLMError`; raw SDK exceptions never escape): `LLMTransientError`
  (`LLMRateLimitError`, `LLMServerError`, `LLMConnectionError`; `retry_after`),
  `LLMRetriesExhaustedError` (`attempts`, `last_error`), `LLMAPIError` (`status_code`),
  `StructuredOutputError`, `RefusalError`, `UnknownRoleError`, `PromptNotFoundError`.
- Tests: `FakeClaude()` scripted with `reply_text`, `reply_tool` (a `str` input stays verbatim
  for malformed JSON), `reply(LLMResponse)`, `fail(error)`; plugs in as
  `get_client(role, transport=fake)` or `fake.client(role, settings=...)` (which also uses
  `no_sleep`); `fake.requests` records every `LLMRequest` (model, effort, system, messages,
  tools, tool_choice, role, prompt_hash); `FakeClaudeExhaustedError` when the script runs out.
  `tests/llm/test_integration.py` is the only real call (`@pytest.mark.integration`).
- `Transport` protocol / `AnthropicTransport`: the only code that imports `anthropic`
  (`tests/llm/test_import_boundary.py` enforces it for the whole package).

The cost ledger and caps (#28) build on `LLMRequest.role`/`prompt_hash` and `LLMResponse.usage`.
