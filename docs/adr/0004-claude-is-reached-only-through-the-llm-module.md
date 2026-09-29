# ADR-0004: Claude is reached only through the llm module

Status: accepted (2026-09-24); model decision amended 2026-09-29

## Decision
- Only `studentassistant.llm` imports the `anthropic` SDK. `studentassistant.llm` is also the
  only code that runs the Claude Code CLI. `[llm] backend` chooses between the Anthropic API
  (`anthropic` SDK) and the locally installed Claude Code CLI running headless on the user's
  subscription (`claude -p` stream-json, one long-lived process per conversation). `auto` uses
  the API when a key is on the machine and Claude Code otherwise. Other modules ask for a **role**
  (`observer`, `transcriber`, `editor`, `generator`) and get a client configured from
  `config.toml` (`[llm.roles.<role>] model = ..., effort = ...`). Default (human decision,
  2026-09-29, replacing Opus 5.5 and Sonnet 5): **`claude-sonnet-5-5` in every role**, with no date
  suffix ($2 input / $10 output / $0.20 cache read per MTok); the roles differ only by effort.
  Its effort defaults to `high`, so effort is always set explicitly, and thinking cannot be
  disabled (`{type: "disabled"}`, `budget_tokens` and non-default `temperature` / `top_p` /
  `top_k` return 400, and so does assistant prefill). Rerunning a crop the student
  complained about uses the same model at a higher effort instead of another model. The Claude
  Code backend passes the same model id.
- Structured outputs use **strict tools with `tool_choice: auto`** plus an instruction, or
  `output_config.format` -- never forced `tool_choice` (`any`/`tool`), which Sonnet 5.5 rejects.
  Every tool input is validated against its Pydantic model before it is applied. Under the
  Claude Code backend, client tools are emulated through a JSON reply protocol and still
  validated against their Pydantic model.
- Under the Claude Code backend, the web search / web fetch server tools of the
  `[sources] web_search_role` client run on the CLI's own `WebSearch` / `WebFetch` built-ins, the
  only built-in tools any process is given. `studentassistant.llm` maps their results back to the
  API's blocks. Every other role still runs with no built-in tools.
- Long inputs/outputs stream. Prompt caching is used deliberately: stable prefix (system prompt,
  tools, topic sources) first, volatile content last; append-only conversations for the observer.
- Every call appends a ledger entry (role, model, prompt hash, input/output/cache tokens, cost
  estimate, topic, session) to the topic's `ledger.jsonl`. Per-session and per-day cost caps are
  configurable.
- Prompts are versioned files under `backend/src/studentassistant/prompts/`; their hash is
  recorded with each call.
- Tests never call the API: `FakeClaude` replays scripted responses; an opt-in
  `@pytest.mark.integration` suite may call the real API.
- The API key, or the Claude Code login, comes from the machine (env/keyring/config/
  `claude auth`), never from the vault.
