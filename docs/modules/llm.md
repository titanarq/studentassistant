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
- The only importer of `anthropic` and the only code that runs the `claude` CLI. No domain logic.

## Configuration

`[llm.roles.<role>]` in `config.toml` (or `SA_LLM__ROLES__<ROLE>__<KEY>`), one table per role
`observer`, `transcriber`, `editor`, `generator`. A partial table keeps that role's defaults.

| key | observer / transcriber | editor / generator |
|---|---|---|
| `model` | `claude-sonnet-5` | `claude-opus-5-5` |
| `effort` (`low`..`max`, always sent) | `medium` | `high` |
| `max_tokens` | `16000` | `64000` |
| `turn_timeout_seconds` | observer `90`, transcriber unset | unset |
| `max_attempts` | observer `2`, transcriber unset | unset |

`[llm] max_attempts = 4`: attempts per call (first one included) before a 429/5xx/connection error
surfaces. A role's own `max_attempts` wins over it (`get_client` resolves it).

Per-role turn timeout and attempts (#409): a hung or very slow call must not stall a live session
(the request detector allows one call per session in flight and keeps only a short window of
finals; an editor turn holds the notes lock), so the observer role (live observer and request
detection) defaults to `turn_timeout_seconds = 90` and `max_attempts = 2`. An unset
`turn_timeout_seconds` keeps the backend's own: `[llm.claude_code] turn_timeout_seconds` (600)
on Claude Code, none of ours on the API (only the SDK's). The client copies the role's value into
`LLMRequest.turn_timeout_seconds` (bookkeeping, never sent); `ClaudeCodeTransport` bounds the turn
with it (killing the process) and `AnthropicTransport` bounds the whole streamed answer with it.
A call past it raises `LLMConnectionError` ("did not answer a <role> ... within N s"), retried by
`LLMClient` up to the role's attempts and then `LLMRetriesExhaustedError`; each failed attempt is
logged with the role (`studentassistant.llm.client`), and a failed call records nothing in the
cost ledger, as before. Example (commented out, the defaults):

```toml
# [llm.roles.observer]
# turn_timeout_seconds = 90
# max_attempts = 2
# [llm.roles.editor]
# turn_timeout_seconds = 600   # unset: [llm.claude_code] turn_timeout_seconds
# max_attempts = 4             # unset: [llm] max_attempts
``` The API key comes from the machine (`ANTHROPIC_API_KEY` or an `ant auth` profile);
`studentassistant serve` exports it from the key file `setup` stores (`llm.api_key_file`, see
`docs/modules/infra.md`) when the environment has none.

`[llm] structured_reasks` (unset by default): how many times an invalid or missing strict-tool
call is re-asked (by `structured` and the observer) before it is given up. Unset, it is the
transport's `default_structured_reasks`: 2 for `ClaudeCodeTransport`, 1 for the API (and any
transport without the attribute, `FakeClaude` included). `LLMClient.structured_reasks` is the
resolved value.

### Backend: the API or Claude Code (`[llm] backend`)

`[llm] backend` (`SA_LLM__BACKEND`) is `api`, `claude-code` or `auto` (default).
`resolve_backend(settings=None, *, environ=None, ant_profile=find_ant_profile)` turns `auto` into
`api` when a key is on the machine (`ANTHROPIC_API_KEY`, a non-empty key in the key file, or an
`ant auth` profile; `api_key_available`) and into `claude-code` otherwise.
`default_transport(settings=None)` is the real transport of the resolved backend
(`AnthropicTransport` or `ClaudeCodeTransport`); `get_client` uses it when no `transport=` is given,
and `serve`, `generate` and `eval` build one per process and hand it to every feature, so nothing
outside this module knows which backend runs.

`ClaudeCodeTransport(settings: ClaudeCodeSettings | None = None, *, web_role=None, spawn=None,
monotonic=...)`
(`claude_code.py`) drives the locally installed Claude Code CLI headless, on the user's
subscription, with no polling and no re-sent conversation:

- One long-lived `claude -p --input-format stream-json --output-format stream-json --verbose
  --include-partial-messages` process per conversation, started with `--model` / `--effort` from
  the request (so from `[llm.roles.<role>]`), the system prompt through `--system-prompt-file`,
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS` = the request's `max_tokens`, `--tools ""` (no built-in tool:
  no file edit, no bash, no web; a web request is the one exception, below), `--strict-mcp-config`, `--setting-sources ""`,
  `--disable-slash-commands`, `--no-session-persistence`, `--safe-mode`, then
  `[llm.claude_code] extra_args`; its working directory is `[llm.claude_code] workdir`.
  A request is one user turn written as a JSON line on stdin; stdout is read line by line up to
  the turn's `result` event.
- A conversation is keyed by model, effort, `max_tokens`, the whole system prompt (tools
  included) and the CLI web tools it was started with. A request whose messages are what an idle process has already seen (its earlier
  messages plus the assistant turn it returned; `cache_control` markers ignored) followed only by
  user turns is sent to that process as just those turns, so the CLI's own prompt cache is reused.
  Anything else starts a new process; an earlier history is rendered as a transcript into its
  first turn. After `idle_timeout_seconds` without a turn a timer closes the process; at most
  `max_processes` live (the least recently used idle one is closed first); a turn longer than
  the request's role `turn_timeout_seconds` (else `[llm.claude_code] turn_timeout_seconds`) kills
  its process. `aclose()` closes them all.
- Client tools (`structured`'s strict tool, the observer's and the editor's tools) are described
  in the system prompt (name, description, input schema) with the instruction to answer a call as
  one JSON object `{"tool_calls": [{"name", "input"}]}`; such a reply becomes `tool_use` blocks
  (ids `toolu_cc_...`, `stop_reason: tool_use`). The call is parsed with
  `json_repair.loads_tolerant` (#320): fences and prose around it are dropped and small defects
  are repaired where `json` reports them, one at a time (a missing `,` between members or
  elements, a trailing `,`, an unescaped `"` inside a string, raw control characters), and an
  `input` written as a JSON string is parsed too. One that is still malformed (e.g. cut off)
  keeps its raw input string so `structured` reports the JSON error and re-asks. `tool_result` blocks go back as text,
  images and documents as blocks.
- Web search / web fetch (#306): `default_transport` passes `web_role` = `[sources]
  web_search_role`. A request of that role carrying the web search / web fetch server tools
  (`web_search_*`, `web_fetch_*`) runs on a process started with `--tools` and `--allowedTools`
  naming only the CLI built-ins that serve them, `[llm.claude_code] web_search_tool` /
  `web_fetch_tool` (`WebSearch` / `WebFetch`); the system prompt tells Claude to use them
  (`max_uses`, allowed / blocked domains; WebFetch with a prompt asking for the page verbatim),
  and the client tools of the same request keep the JSON protocol. The CLI's `tool_use` /
  `tool_result` events (with their `tool_use_result`) come back as the API's blocks, ahead of the
  answer's own: `server_tool_use` (`web_search` `{query}` / `web_fetch` `{url}`),
  `web_search_tool_result` (the found `{url, title}` pages, or an error object) and
  `web_fetch_tool_result` (a text `document` with the page, `retrieved_at` = the event's time, or
  error `fetch_failed`), so `parse_web_results` and `sources` do not branch on the backend.
  Usage: `web_search_requests` is the `result` event's count (`usage.server_tool_use`, else the
  sum of `modelUsage.*.webSearchRequests`, where the CLI reports it), `web_fetch_requests` the
  WebFetch calls seen (the CLI reports none); the turn is billed and capped like any other
  (`billing: subscription`, the reported cost). What the CLI cannot serve: a web tool whose name
  is empty in config raises `WebToolsUnavailableError` (an `LLMAPIError`, not retried) before any
  process starts; a CLI whose `init` event does not list the tool raises it too (the process is
  dropped); a web fetch request whose turn brought no page text back gets a
  `web_fetch_tool_result` error `WEB_TOOL_UNAVAILABLE_ERROR` (`web_tool_unavailable`). Server
  tools in any other role's request, or other server tools, still raise `LLMAPIError` before
  anything starts. Known limit: the CLI's WebFetch returns the page as its helper model renders it
  for the prompt (Markdown, possibly cut on a very long page), not the raw document the API's web
  fetch returns.
- `on_text` gets the text deltas (`stream_event` `text_delta`). With tools offered, the text is
  held back from where a tool call may start (a line opening with `{` or a fence, or
  `{"tool_calls"`): the reply written before a call streams, the call itself (which carries the
  whole change, e.g. a Mermaid diagram) never does; held text that is not a call is sent at the end.
- Usage is the `result` event's `usage`; the turn's cost is the increase of the process's running
  `total_cost_usd`, returned as `LLMResponse.reported_usd`, with `LLMResponse.billing` =
  `subscription` (`api` when the CLI's `init` event names an API key source). The ledger records
  `reported_usd` as `estimated_usd` (the price table when absent) and `billing: "subscription"`;
  the caps apply to it as to any entry. On a subscription it is the CLI's list-price equivalent,
  not money charged.
- Errors: a `result` with `is_error` maps `api_error_status` 429 to `LLMRateLimitError`, 5xx to
  `LLMServerError`, anything else to `LLMAPIError`; a process that exits before answering or
  times out is an `LLMConnectionError` (retried by `LLMClient` on a fresh process); a missing
  executable is an `LLMAPIError`. A failed or cancelled turn drops its process.
- `check_claude_code(settings=None, *, which=shutil.which, run=...) -> ClaudeCodeStatus`
  (`executable`, `logged_in`, `auth_method`, `subscription_type`): one `claude auth status --json`
  bounded by `auth_check_timeout_seconds`, no model call, for `doctor`.

`[llm.claude_code]` (`SA_LLM__CLAUDE_CODE__<KEY>`):

| key | default |
|---|---|
| `executable` | `claude` |
| `extra_args` | `[]` |
| `idle_timeout_seconds` | `600` |
| `max_processes` | `6` |
| `turn_timeout_seconds` | `600` |
| `auth_check_timeout_seconds` | `20` |
| `workdir` | `~/.cache/studentassistant/claude-code` |
| `web_search_tool` | `WebSearch` (the CLI tool a web search runs on; `""` turns web search off under this backend) |
| `web_fetch_tool` | `WebFetch` (the same for a web fetch) |

Tests drive it with a fake `claude` script (`tests/llm/fake_claude_cli.py`, with scripted
WebSearch / WebFetch tool events: `web_search(...)`, `web_fetch(...)`), never the real CLI.

Cost (every key also `SA_LLM__<KEY>`, e.g. `SA_LLM__MAX_USD_PER_DAY=5`,
`SA_LLM__PRICES__claude-sonnet-5__INPUT_PER_MTOK=2`):

- `[llm] max_usd_per_session`, `max_usd_per_day` (floats, USD): unset = no cap. The day is the
  UTC day, so the day cap resets at UTC midnight.
- `[llm.prices."<model id>"]` with `input_per_mtok`, `output_per_mtok`, `cache_write_per_mtok`
  (5-minute TTL, the one this module sets), `cache_read_per_mtok`, all USD per million tokens.
  A configured table is merged over the defaults per model and per key.

| model | input | output | cache write | cache read |
|---|---|---|---|---|
| `claude-sonnet-5` | 2.00 | 10.00 | 2.50 | 0.20 |
| `claude-opus-5-5` | 4.00 | 20.00 | 5.00 | 0.20 |

No price or cap lives anywhere but these config defaults.

## Public surface (`from studentassistant.llm import ...`)

- `get_client(role, *, settings=None, transport=None, sleep=asyncio.sleep, ledger=None,
  clock=utc_now) -> LLMClient`; `UnknownRoleError` for any other role.
  `LLMClient.create(messages, *, system=None, tools=None, tool_choice=None, max_tokens=None,
  cache=True, prompt_hash=None, confirm_over_cap=False, on_text=None) -> LLMResponse` (async).
  `on_text` (a `TextSink`, `async (delta: str) -> None`) receives the answer's text deltas as they
  stream in (`stream.text_stream`), for a live reply; the return value is still the whole final
  message, and after a transport retry the deltas start again. The client passes `on_text` to
  `Transport.send(request, on_text=...)` only when given, so a transport with a plain
  `send(request)` keeps working; `FakeClaude` streams its scripted text blocks word by word.
  Every call streams (`messages.stream` + `get_final_message`), sends `output_config.effort`,
  never sends `thinking`, and refuses a forced `tool_choice` (`any`/`tool`) with `ValueError`.
  `build_request(...)` returns the `LLMRequest` without sending it.
- `LLMResponse`: `content` (raw blocks; `assistant_turn()` to append it back), `text`,
  `tool_calls` (`ToolCall.input_json`, `parsed_input()`: tolerant, raises `json.JSONDecodeError`
  only for unusable JSON), `stop_reason`, `usage`
  (input/output/cache-creation/cache-read tokens), `model`.
- Caching: with `cache=True` (default) the last system block (or, with no system, the last
  tool) gets `cache_control: ephemeral`, so tools + system form the cached prefix and the messages
  stay volatile. `cached_block(text)` marks an extra breakpoint (e.g. topic sources);
  `cache_stable_prefix`, `system_blocks` are the underlying helpers.
- `structured(client, messages, OutputModel, *, tool_name, tool_description, system=None,
  max_tokens=None, prompt_hash=None, confirm_over_cap=False) -> StructuredResult` (`.value`, `.responses`): a strict tool
  (`strict_tool`) with `tool_choice: auto` and the `structured-output` prompt as instruction;
  the input is parsed with `ToolCall.parsed_input()` (`loads_tolerant`, so a repairable defect
  is no error) and always validated with Pydantic; `client.structured_reasks` re-asks carrying
  the error (1 on the API, 2 on claude-code), then `StructuredOutputError`. `RefusalError` on `stop_reason: refusal`.
- Server-side web tools (`web.py`): `web_search_tool(settings=None, *, max_uses=None,
  allowed_domains=None, blocked_domains=None)` and `web_fetch_tool(settings=None, *, max_uses=None,
  max_content_tokens=None)` are the tool definitions, their `type` from `[llm] web_search_tool` /
  `web_fetch_tool` (defaults `web_search_20260209` / `web_fetch_20260209`, the dynamic-filtering
  versions; never declare `code_execution` next to them). `run_server_tools(client, messages, *,
  max_continuations=5, **create) -> ServerToolRun` (`responses`, `messages`, `final`, `content`)
  calls `create` and, while the answer stops with `pause_turn`, sends it again with the paused
  assistant turn appended (no extra user message), each call its own ledger entry.
  `parse_web_results(content) -> WebToolResults` reads the blocks: `queries` (from
  `server_tool_use`), `hits` (`WebSearchHit`: `url`, `title`, `page_age`, each URL once),
  `documents` (`FetchedDocument`: `url`, `title`, `retrieved_at`, `media_type`, `text` for a text
  document, `data` for a base64 one such as a PDF) and the `error_code`s of failed searches and
  fetches (a server tool error is an HTTP 200 whose result block holds an error object).
  `Usage.web_search_requests` / `web_fetch_requests` come from `usage.server_tool_use`; each web
  search adds `[llm] web_search_usd_per_thousand` (default 10.0 USD per 1,000) / 1000 to
  `estimate_usd(..., web_search_usd_per_thousand=)` and so to the ledger entry and the caps; a
  fetch costs only its tokens. Tests script them with `web_search_blocks(query, hits, *,
  error_code=None)` and `web_fetch_blocks(url, text, *, title, media_type, data, error_code)`
  inside `FakeClaude.reply(LLMResponse(content=...))`.
- `check_api_key(api_key=None, *, timeout=15.0, sdk=None)` -- one free, unretried
  `GET /v1/models?limit=1` (no tokens, no ledger, no cap) proving a key works, for
  `studentassistant doctor --api-call`; raises `LLMAPIError` (401/403 = refused) or
  `LLMTransientError`/`LLMError` like any call. `sdk` replaces the `anthropic.Anthropic` client.
- `find_ant_profile(environ=None, home=None) -> str | None` -- the `ant auth` profile the SDK
  would use (`ANTHROPIC_PROFILE`, else `<config_dir>/active_config`, else `default`, with
  `<config_dir>` = `ANTHROPIC_CONFIG_DIR` or `~/.config/anthropic`) when its
  `configs/<profile>.json` exists; never opens `credentials/` nor runs `ant`. For `doctor`.
- Cost ledger: `LedgerBinding(vault, subject, topic, session=None)` passed as `ledger=` to
  `get_client` / `FakeClaude.client` makes every successful `create` append a `LedgerEntry`
  through `studentassistant.vault.append_ledger_entry` (time from `clock`, role, model, prompt
  hash, input/output/cache-read/cache-write tokens, `estimated_usd`, subject, topic, session);
  `structured` records one entry per underlying call, the re-ask included. A failed call records
  nothing; a ledger that cannot be written is logged and the answer is still returned. Without a
  binding nothing is recorded and no cap applies; calls with no topic are never recorded.
- `estimate_usd(usage, model, prices) -> float | None`: `None` for a model missing from
  `[llm.prices]`, with a warning logged once per model; such an entry keeps its token counts and
  adds 0 to the capped totals (`studentassistant cost` counts it as "sin precio conocido").
- Caps, checked before each bound call: the session total is the bound session's entries in its
  topic's ledger (no session bound = no session cap), the day total every entry of the vault whose
  `time` falls on the current UTC day. At or over either cap, `observer`/`transcriber` calls raise
  `CostCapReachedError` (paused, whatever `confirm_over_cap` says) and `editor`/`generator`
  calls raise `CostConfirmationRequiredError` unless `confirm_over_cap=True`, which proceeds and
  records normally. Both derive from `CostCapError(LLMError)` with `cap` (`"session"` checked
  first, then `"day"`), `limit_usd` and `total_usd`; nothing is sent when they are raised.
- `cost_status(binding, settings=None, *, now=None) -> CostStatus` (`session_usd`, `day_usd`,
  `max_usd_per_session`, `max_usd_per_day`, `observer_paused`, `editor_needs_confirmation`, the
  last two true once either cap is reached; `unpriced_session_calls`, `unpriced_day_calls` and
  `unpriced_models` count the entries of each scope with no known cost) for the server's
  `GET /api/cost`.
- CLI: `studentassistant cost [--topic <subject-slug>/<topic-slug>]` prints, from the configured
  vault, USD and tokens per topic with spend (or the one topic) plus `Hoy (UTC)`; an unknown or
  malformed topic exits 1 with a Spanish message.
- Prompts: `load_prompt(name) -> Prompt` (`name`, `content`, `hash` = `sha256:<hex>`, `render`)
  for `backend/src/studentassistant/prompts/<name>.md`; `PromptRegistry(directory)` (`names`,
  `get`); `content_hash`. Missing prompt: `PromptNotFoundError`.
- Errors (all subclass `LLMError`; raw SDK exceptions never escape): `LLMTransientError`
  (`LLMRateLimitError`, `LLMServerError`, `LLMConnectionError`; `retry_after`),
  `LLMRetriesExhaustedError` (`attempts`, `last_error`), `LLMAPIError` (`status_code`),
  `StructuredOutputError`, `RefusalError`, `UnknownRoleError`, `PromptNotFoundError`,
  `CostCapReachedError`, `CostConfirmationRequiredError`.
- Tests: `FakeClaude()` scripted with `reply_text`, `reply_tool` (a `str` input stays verbatim
  for malformed JSON), `reply(LLMResponse)`, `fail(error)`; plugs in as
  `get_client(role, transport=fake)` or `fake.client(role, settings=...)` (which also uses
  `no_sleep`); `fake.requests` records every `LLMRequest` (model, effort, system, messages,
  tools, tool_choice, role, prompt_hash); `FakeClaudeExhaustedError` when the script runs out.
  `tests/llm/test_integration.py` is the only real call (`@pytest.mark.integration`).
- `LLMResponse.billing` (`api` | `subscription`) and `reported_usd` (see "Backend" above).
- `Transport` protocol / `AnthropicTransport`: the only code that imports `anthropic`
  (`tests/llm/test_import_boundary.py` enforces it for the whole package).

The cost ledger and caps build on `LLMRequest.role`/`prompt_hash` and `LLMResponse.usage`
(`input_tokens` are the uncached ones; cache writes and reads are reported apart).
