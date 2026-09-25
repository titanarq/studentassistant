# Module: generators

**Lives in:** `backend/src/studentassistant/generators/`.

## Responsibility
From the master notes (+ sources for context), role `generator`: outline (+ mermaid mind map),
quiz (YAML: type, question, options, answer, explanation, source ref, difficulty), flashcards
(+ Anki `.apkg`, CSV), exercises and mock exam with solutions (printable PDF), slides (Marp
Markdown -> PDF/PPTX). Every artifact records the notes version it was built from and is marked
stale when the notes change. Every item keeps provenance.

## The framework (#67)

A generator is a `Generator` subclass registered under a `kind`; the framework runs it over a
topic's `notes/apuntes.md`, stores what it returns under `generated/` through
`studentassistant.vault`, and tells when it is stale. Generators never write the vault nor run git.

### Writing a generator -- `base.py`, `registry.py`

```python
from studentassistant.generators import Generator, GeneratorContext, GeneratorOutput
from studentassistant.generators import ItemProvenance, register

@register                                # on generators.default_registry
class OutlineGenerator(Generator):
    kind = "esquema"                     # slug [a-z0-9-]; CLI/API name and file stem
    title = "Esquema"                    # Spanish, for the student
    version = 1                          # bump when the output changes shape
    options_model = OutlineOptions       # a Pydantic model; NoOptions by default

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        result = await context.structured(
            [{"role": "user", "content": [context.notes_block()]}], Outline,
            tool_name="record_outline", tool_description="...",
            system=prompt.content, prompt_hash=prompt.hash,
        )
        return GeneratorOutput(
            files={"esquema.md": render(result.value)},
            items=[ItemProvenance(item="n1", anchors=["definicion"]), ...],
            model=result.responses[-1].model, prompt_hash=prompt.hash,
        )
```

The built-in generator modules are imported by `studentassistant.generators/__init__.py` so they
register themselves.

- `GeneratorContext`: `vault`, `subject`, `topic`, `topic_title`, `notes_text`, `notes` (the
  parsed `NotesDocument` of `editor.notes_format`), `sections` (`NoteSection`: `anchor`, `title`,
  `level`) and `anchors`, `basis` (`NotesBasis`, below), `options` (validated), `client` (a
  `generator` `LLMClient` bound to the topic's ledger), `kind`, `confirm_over_cap`.
  `notes_block()` is a text block with the notes, their version and the anchors to cite;
  `structured(messages, output, *, tool_name, tool_description, system, prompt_hash,
  max_tokens)` is `llm.structured` on the context's client (the cost-cap confirmation passed on),
  recording the turns in `conversations/generator.jsonl`; `record(kind, **fields)` appends a
  record there.
- `GeneratorOutput`: `files` (name relative to `generated/` -> `str | bytes`; every name starts
  with `<kind>.`, `<kind>-` or `<kind>/`, and none ends in `.meta.yaml`), `items`
  (`ItemProvenance`: `item` id, `anchors` without `#`), `warnings` (Spanish), `model`,
  `prompt_hash`.
- `GeneratorRegistry`: `register(cls)` (decorator; a bad kind, a missing title or a second class
  for a kind is `ValueError`), `unregister(kind)`, `kinds()`, `classes()`, `lookup(kind)`,
  `kind in registry`, `get(kind)` (a new instance, `UnknownGeneratorError` with a Spanish message
  listing the known kinds). `default_registry` / `register` are the process-wide ones.

### Running -- `run.py`

`await run_generator(vault, subject, topic, kind, *, client, sync, registry=default_registry,
options=None, confirm_over_cap=False, clock=...) -> GenerateResult`:

1. the generator of `kind` (`UnknownGeneratorError`); `options` validated against its
   `options_model`, unknown keys refused (`InvalidOptionsError`, Spanish);
2. `notes/apuntes.md` read (`NoNotesError` when there is none) and its `NotesBasis` taken:
   `sha256` of the text, `version`/`tag` of the newest `apuntes-vN`, `changed_since_version`;
3. the generator runs; its files are checked (`GeneratorOutputError`: no files, a name outside
   its kind, a manifest name). Claude errors (`CostConfirmationRequiredError`, `RefusalError`,
   `LLMError`) propagate and nothing is written;
4. items with no anchor, or anchors the notes lack, are **reported, not dropped**: listed in
   `unresolved` (with the missing anchors) and in a Spanish warning;
5. stored: files the previous run of the kind wrote and this one did not are removed, the files
   written, then the manifest `generated/<kind>.meta.yaml` (`ArtifactMeta`: `kind`,
   `generator_version`, `built_at`, `model`, `prompt_hash`, `options`, `notes` (the basis),
   `files`, `items`, `unresolved`, `warnings`), and one commit `Generar <kind> de <s>/<t>
   (apuntes vN)`. A final `material.generated` record goes to `conversations/generator.jsonl`.

`GenerateResult`: `subject`, `topic`, `kind`, `files` (vault-relative, the manifest last),
`removed`, `notes`, `commit`, `items` (count), `unresolved`, `warnings`, `model`.

### Stale detection

Computed on read, never stored: `artifact_status(vault, subject, topic, kind, *, registry)` ->
`ArtifactStatus` (`kind`, `title` (none for a kind no longer registered), `generated`, `stale`,
`stale_reason` (Spanish), `files` (vault-relative), `meta`). Stale when the current `apuntes.md`
is gone or its SHA-256 is not the manifest's (a revision, a restore, a doubt's edit; going back to
the same text is fresh again), when the registered generator's `version` is newer than the
manifest's, or when the manifest cannot be read. `materials_status(vault, subject, topic, *,
registry)` -> `MaterialsStatus` (`has_notes`, `notes_sha256`, `artifacts`: every registered kind,
sorted, then any other kind with a manifest). `read_artifact_meta(...)` reads one manifest
(`GenerationError` when unreadable).

### CLI and API

- `studentassistant generate <kind> --topic <subject>/<topic> [-o key=value ...]
  [--confirm-over-cap]`: runs in-process on the configured vault with the real transport, prints
  the files, the notes version and the warnings; the commit is local and the server's sync pushes
  it. Values of `-o` are JSON when they parse (`size=10`, `split=true`), text otherwise.
- REST (`server/generators_routes.py`, see `docs/modules/server.md`): `GET /api/generators`,
  `GET .../topics/{t}/generated`, `POST .../topics/{t}/generated/{kind}`.

## Outline -- kind `esquema` (#74)

`generators/outline.py`; `OutlineGenerator` (title `Esquema`, version 1, no options) is registered
on `default_registry`, so `studentassistant generate esquema --topic <s>/<t>` and
`POST .../generated/esquema` run it. It writes one file, `generated/esquema.md`, plus the
framework's `generated/esquema.meta.yaml` (notes version, provenance, stale marking).

- Claude (role `generator`, prompt `prompts/generator_outline.v1.md`, tool `record_outline`)
  answers an `OutlineDraft`: a **flat** list of `OutlineDraftNode` (`id`, `parent` -- an earlier
  node's id or null --, `title`, `gloss` or null, `anchors`), because strict tools refuse
  recursive schemas. The draft is checked (unique ids, parents before children, at most
  `MAX_DEPTH` = 4 levels, no empty title); a draft that fails is re-asked once by
  `llm.structured`. `OutlineDraft.to_outline(title)` builds the tree.
- `Outline` (`title` -- the topic's, the mind map's root --, `nodes`) and `OutlineNode` (`title`,
  `gloss`, `anchors` without `#`, `children`): titles and glosses are one line, an empty title or
  more than `MAX_DEPTH` levels is a `ValidationError`. `walk()` yields `(number, level, node)` in
  document order (`"1"`, `"1.2"`, `"1.2.1"`); `provenance()` is one `ItemProvenance` per node,
  named by that number.
- `render_outline(outline, *, known_anchors=None) -> str` (pure): `# Esquema: <tema>`, top-level
  nodes as `## 1. Título` with their gloss and `Apuntes:` links (`../notes/apuntes.md#<anchor>`),
  deeper nodes as nested bullets `- **1.2 Título**: glosa · [#ancla](...)`, then `## Mapa mental`
  with `render_mindmap(outline)`: one fenced `mermaid` `mindmap` block (`root(("Tema"))`, level 1
  `n1("...")`, deeper `n1_2["..."]`). `mermaid_label(text)` quotes each label and replaces what
  mermaid would misread (`"` and backtick -> `'`, `<...>` -> `‹...›`, `#name;` entity codes,
  `%%`); `<` in the Markdown body is written `&lt;`.
- Provenance: a node with no anchor, or citing an anchor the notes lack, is kept, marked in the
  Markdown (`*(sin sección de los apuntes)*`, `` `#x` *(no está en los apuntes)* ``) and reported
  by the framework in `unresolved` and a warning.
- Golden rendering: `backend/tests/fixtures/generators/esquema.md` (open it on GitHub to see the
  mind map).
