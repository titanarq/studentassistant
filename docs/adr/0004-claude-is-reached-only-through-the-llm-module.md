# ADR-0004: Claude is reached only through the llm module

Status: accepted (2026-09-24)

## Decision
- Only `studentassistant.llm` imports the `anthropic` SDK. Other modules ask for a **role**
  (`observer`, `transcriber`, `editor`, `generator`) and get a client configured from
  `config.toml` (`[llm.roles.<role>] model = ..., effort = ...`). Defaults: `claude-sonnet-5`
  for observer and page transcription, `claude-opus-5-5` for editor and generators (better
  and cheaper than Opus 5; its effort defaults to `medium`, so effort is always set explicitly,
  and thinking cannot be disabled).
- Structured outputs use **strict tools with `tool_choice: auto`** plus an instruction, or
  `output_config.format` -- never forced `tool_choice` (`any`/`tool`), which Opus 5.5 rejects.
  Every tool input is validated against its Pydantic model before it is applied.
- Long inputs/outputs stream. Prompt caching is used deliberately: stable prefix (system prompt,
  tools, topic sources) first, volatile content last; append-only conversations for the observer.
- Every call appends a ledger entry (role, model, prompt hash, input/output/cache tokens, cost
  estimate, topic, session) to the topic's `ledger.jsonl`. Per-session and per-day cost caps are
  configurable.
- Prompts are versioned files under `backend/src/studentassistant/prompts/`; their hash is
  recorded with each call.
- Tests never call the API: `FakeClaude` replays scripted responses; an opt-in
  `@pytest.mark.integration` suite may call the real API.
- The API key comes from the machine (env/keyring/config), never from the vault.
