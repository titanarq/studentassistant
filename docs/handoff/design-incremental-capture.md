# Design: building the notes from successive, overlapping captures

## What happened (real vault, lengua/la-comunicacion, 2026-09-26..28)
Nine captures of what is really one notebook page (pages 1-6 the same list; 7-8 repeat its last
line, CONTEXTO, then add the Repaso scheme). (a) The whole-topic generation wrote the list once per
reading plus a section «Lecturas distintas entre las fotos»; the student deleted the document.
(b) «Añade el diagrama de la imagen que he seleccionado» (page 8, a revise turn with a Recursos
selection) re-added framing text; the student: «es una página repetida, no añadas contenido
repetido». (c) After the student had settled page 6 (doubt «ondas sonoras» answered), the
transcriber's doubts on page 8's copy of the same CONTEXTO line («revista», «horrores») were
asked in the chat although the notes did not even hold that example («los apuntes no cambian»).
Nothing in the editor knows that a capture overlaps content already incorporated, nor which
blocks the student already reviewed.

## Choice: a build mode of the existing tutor-editor, not a new agent
The alignment needs exactly what the editor call that writes already has: the current notes, their
block map with provenance, the new capture and its doubts. A separate "construction assistant"
(Sonnet or Opus) would add one more call per capture, a second writer of `apuntes.md` racing the
notes lock, and its verdict would still have to be enforced by the same validator. So: rules in
the editor's prompts + a small derived state + deterministic checks. No new role, no change in
`llm`, `vault`, `observer`, `protocol` or `web`.

## Rules
1. **Overlap-aware merge** (prompts `editor_incorporate`, `editor_revise` when the turn has a
   selection, one line in `editor_generate`): align each new capture against the notes; what the
   notes already say (same idea, even read slightly differently) is not written again; only the
   genuinely new remainder is fitted in; a capture that only repeats is `nothing_new`.
2. **Replace only when clearly better and not settled.** "Clearly better" = the earlier text of
   that fragment had a gap, an uncertain reading (`[[?`, an open doubt), was cut at the page edge
   or was left out as illegible, and the new capture reads it with no uncertainty mark; a
   different wording alone is never better. The input gives the facts per capture (the requested
   ones and those the notes cite): uncertain marks in its transcription, sharpness, triage flags.
3. **Settled blocks are locked.** A block is *settled* when the student reviewed it (below), it
   has no `[[?` and no open doubt names a source it cites. The block map marks it «[revisado]».
   An incorporation that changes (text modulo footnote refs) or deletes a settled block is
   re-asked; the doubts review auto-resolves an open doubt about content a settled block already
   holds (evidence: its source) and never asks it.
4. **Doubts across captures only when they come together.** A request with several captures may
   raise a contradiction between them as today; a contradiction between a requested capture and
   an already-incorporated capture of the same kind (notes/notes, book/book) is re-asked: keep
   the notes' reading or replace it under rule 2. Notes vs book stays allowed.

## "Reviewed" (minimal addition)
No per-block review state exists. New append-only record `notes.reviewed` in
`conversations/editor.jsonl` (existing conversation API; no vault layout change): `reason`
(`student_edit` | `doubt_closed`), `blocks` (keys = SHA-256 of the block text without footnote
refs, whitespace collapsed). Written when the student saves (the blocks they added or changed)
and when the student answers or dismisses a doubt (the blocks then citing its sources). Editing a
block changes its key, so it is no longer reviewed until reviewed again. New module
`editor/reviewed.py`: `block_key`, `reviewed_keys`, `settled_blocks(vault, s, t, notes)`.
Follow-up (separate issue): an explicit chat confirmation («esta sección está bien»).

## Eval coverage
`evals.scoring`: new pure score *unique* (1 - share of generated units repeating an earlier
generated unit), in the report and comparison, not in the global mean (keeps baselines).
Fixture `tests/fixtures/overlap/`: synthetic overlapping page transcriptions + reference notes,
used by the scoring test and FakeClaude editor tests (lock, contradiction refusal, block map
marks, reviewed records). The real session can become an eval case with `eval import`.
