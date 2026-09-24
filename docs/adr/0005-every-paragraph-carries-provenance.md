# ADR-0005: Every paragraph carries provenance

Status: accepted (2026-09-24)

## Decision
- The master notes (`notes/apuntes.md`) are Markdown with stable section anchors
  (`## 2. Causas {#causas}` style ids kept across edits).
- Every paragraph, list item group or table ends with one or more **provenance footnotes**
  pointing to vault-relative source ids: a notes/book page (`sources/notes/page-004.jpg`), a
  transcript span (`sessions/<id>#t=00:02:34-00:03:10`), a PDF page, a web snapshot. Footnote
  definitions are real links so they work when the vault is browsed on GitHub.
- Content with no support in the student's sources carries the special footnote `[^ia]`
  ("Ampliado por la IA: no está en tus fuentes").
- A topic has a **fidelity mode**: `estricto` (no `[^ia]` content allowed; the editor must ask
  instead) or `ampliado` (allowed, always marked). Default `estricto`.
- A validator in `studentassistant.editor` rejects any editor output that breaks these rules;
  the editor is re-asked, never silently patched.
- Contradictions between sources are resolved by the student; the decision and, if requested, a
  note with the discarded version are kept.
