# Module: generators

**Lives in:** `backend/src/studentassistant/generators/`.

## Responsibility
From the master notes (+ sources for context), role `generator`: outline (+ mermaid mind map),
quiz (YAML: type, question, options, answer, explanation, source ref, difficulty), flashcards
(+ Anki `.apkg`, CSV), exercises and mock exam with solutions (printable PDF), slides (Marp
Markdown -> PDF/PPTX). Every artifact records the notes version it was built from and is marked
stale when the notes change. Every item keeps provenance.
