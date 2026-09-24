# Module: editor

**Lives in:** `backend/src/studentassistant/editor/`. Decision: ADR-0005.

## Responsibility
- Notes format: parser/validator of `apuntes.md` (anchors, provenance footnotes, `[^ia]`,
  fidelity mode).
- Generation ("prepárame el tema", role `editor`, Opus): from page transcriptions (+ images for
  schemes), the transcript grouped by section, other sources, pending items, digest and the
  subject style guide.
- Edit loop: chat reply + section-level edit ops, validated, applied, committed with a summary;
  conversation persisted.
- Doubts resolution: auto-resolve pending items with cited evidence, ask the rest one by one,
  record decisions.
- "¿Por qué pusiste esto?": explain a paragraph from its cited sources.
- Style guide learning per subject; notes versions (git tags) and diffs.
