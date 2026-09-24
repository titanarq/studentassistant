# ADR-0006: Voice commands are deterministic

Status: accepted (2026-09-24)

## Decision
- Commands (capture, next page, important, switch source book/notes/pdf, pause/resume, end
  session and prepare the topic, web search request) are detected by a **configurable phrase
  grammar** (YAML, Spanish, with variants) over partial and final transcript text, with debounce
  so one utterance fires once.
- Every command also has a button on the phone. A command never depends on an LLM call; the
  observer may *interpret* what was said around it, but never decides whether a command ran.
- The backend acknowledges a command to the phone (haptic + sound on capture) so the student
  knows it happened.
