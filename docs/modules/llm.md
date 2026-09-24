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
