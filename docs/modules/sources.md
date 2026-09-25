# Module: sources

**Lives in:** `backend/src/studentassistant/sources/`.

## Responsibility
- Capture processing: pick the sharpest still of a burst (Laplacian variance), downscale, page
  crop/deskew, store both through `vault`, link to the transcript window around the capture.
- Page transcription (role `transcriber`, vision): page -> Markdown keeping structure, schemes as
  nested lists or mermaid, uncertain words as `[[?word]]`, using spoken hints around the capture
  time; uncertain words become pending items.
- Textbook pages (printed-text prompt, page-number detection), PDF import (page ranges, PyMuPDF
  text), web search and URL snapshots (Markdown + URL + fetch date).

## Boundaries
- Claude only via `llm`; storage only via `vault`.

## Public surface (`from studentassistant.sources import ...`)

What exists today, after issues #34 and #44: PDF import and capture processing.

### Capture processing -- `captures.py`
- `store_capture(vault, subject_slug, topic_slug, kind, stills, meta, session_t_ms, settings)
  -> StoredCapture` processes one burst (`stills`: `BurstStill(data, content_type)` in burst
  order) and stores it through one `vault.put_source` call as a page of `sources/<kind>/`:
  - `page-NNN.jpg`: the sharpest still (variance of the Laplacian on a grayscale copy at a common
    1200 px long edge; ties go to the frame nearest the middle, `len // 2`, the earlier one when
    two are equally near), downscaled to `capture_long_edge` and re-encoded as JPEG at
    `capture_jpeg_quality`. Stills that do not decode are skipped; if none decodes,
    `CaptureImageError` (Spanish message) and nothing is written.
  - `page-NNN.page.jpg`: the page image. The largest convex quadrilateral covering 20-98 % of
    the still, with at most one corner on the image border and clearly lighter than what surrounds
    it (Canny edges and an Otsu threshold both searched), is warped flat by a perspective
    transform; then a mild CLAHE contrast boost on lightness. No page found: the uncropped still
    with the same contrast boost.
  - `page-NNN.burst<K>.<ext>`: every other still exactly as uploaded, `K` its 1-based position in
    the burst (the retention purge's `burst-original` target).
  - `page-NNN.yaml`: `meta` plus `width_px`/`height_px` (of `page-NNN.jpg`), `session_t_ms`,
    `transcript_window: {t_start, t_end}` (session ms; `capture_window_before_seconds` before to
    `capture_window_after_seconds` after, never below 0) -- the span of `transcript.jsonl` a page
    transcription reads as spoken hints -- `selected_image` (the kept still's `K`), `sharpness`
    (per still, `null` for one that did not decode) and `page_detected`.
  `StoredCapture` has `path` (`page-NNN.jpg`), `page_path` and `processed`.
- `process_burst(stills: Sequence[bytes], settings) -> ProcessedBurst` (`selected`, `sharpness`,
  `still`, `page`, `page_detected`, `width_px`, `height_px`) is the same processing without
  storing; `transcript_window(session_t_ms, settings) -> {"t_start", "t_end"}`.
- The building blocks (`decode_image`, `sharpness`, `pick_sharpest`, `downscale`, `find_page`,
  `crop_page`, `enhance_contrast`) are importable from `studentassistant.sources.captures`.
- Everything is CPU-bound (OpenCV, `opencv-python-headless`): the capture upload
  (`server/captures.py`) runs `store_capture` in a worker thread.

### PDF import -- `pdf.py`
- `import_pdf(vault, subject_slug, topic_slug, name, content, *, pages=None, settings=None,
  now=None) -> ImportedPdf` stores the PDF `content` (called `name`), or only `pages` (a
  `PageRange(first, last)`, 1-based and inclusive, in the original's numbering), through
  `vault.put_source` as `sources/pdf/page-NNN.pdf`: a new PDF holding just the kept pages. Next to
  it, per kept page `K` (counted in the stored file), `page-NNN.pKKK.txt` (PyMuPDF text; empty for
  a scanned page) and `page-NNN.pKKK.jpg` (thumbnail, long edge `pdf_thumbnail_long_edge`, JPEG
  `pdf_thumbnail_quality`). The sidecar holds `original_name`, `imported_at`, `original_sha256`,
  `original_page_count`, `first_page`, `last_page`, `page_count`. `ImportedPdf` has `path`,
  `source_id` (`sources/pdf/page-NNN.pdf`), `meta` and `pages` (`ImportedPage`: `page`,
  `original_page`, `source_id` `...#page=K`, `text_path`, `image_path`, `has_text`).
- `parse_page_range(text) -> PageRange` reads `82-94`, `82–94`, `páginas 82 a 94`, `pág. 7`.
- Refusals (all `PdfImportError`, Spanish messages, nothing written): `PdfTooLargeError`,
  `PdfUnreadableError` (not a PDF, empty, or password-protected), `PageRangeError` (malformed,
  backwards or outside the PDF). A page text or the PDF that looks like a key raises the vault's
  `SecretRefused`.
- Page numbers: provenance cites `sources/pdf/page-NNN.pdf#page=K` with `K` in the stored file --
  what the link opens on GitHub and what Claude's citations count. `original_page(meta, K)` is the
  page the student knows (`first_page + K - 1`). `pdf_page_count(vault, s, t, source_id)` and
  `pdf_has_page(vault, s, t, source_id)` read the sidecar; the editor's `topic_source_resolver`
  uses the latter so a cited page outside the import is reported.
- Editor input: `pdf_document_block(vault, s, t, source_id, *, cache=False) -> dict` is a Claude
  `document` block (base64 `application/pdf`, `title` = original name, `context` naming the kept
  original pages, `citations: {enabled: true}`, `cache_control` with `cache=True`), to be placed
  in a user message before the question. `citation_source_id(source_id, citation)` maps a
  `page_location` citation of the answer to `sources/pdf/page-NNN.pdf#page=<start_page_number>`
  (`None` for other citation kinds).
- CLI: `studentassistant import-pdf <subject-slug>/<topic-slug> <file.pdf> [--pages 82-94]`
  imports into the configured vault and commits locally (`GitSync.checkpoint`, message
  `Importar PDF <file> en <subject>/<topic>`); the server's sync pushes it. Exit 1 with a Spanish
  message on any refusal, 2 on a malformed topic.

Import is CPU-bound (PyMuPDF): a server caller runs it in a worker thread, as the web upload
(`POST /api/subjects/{s}/topics/{t}/sources/pdf`, `server/pdf_upload.py`) does.

### Size limit and storage policy
The vault keeps PDFs in plain git, never Git LFS (ADR-0002 leaves LFS an opt-in for later), and
the stored PDF is sent base64 (4/3 of its size) to Claude, whose requests are capped at 32 MB. So:

| `[sources]` key (`SA_SOURCES__*`) | default | beyond it |
|---|---|---|
| `max_pdf_bytes` | 200 MiB | the PDF is refused before it is opened (the CLI checks the file size before reading it) |
| `max_pdf_pages` | 100 | a range with more pages is refused, never truncated: choose a shorter range |
| `max_stored_pdf_bytes` | 20 MiB | the kept pages as a PDF are refused: choose a shorter range |
| `pdf_thumbnail_long_edge` | 1200 px | -- |
| `pdf_thumbnail_quality` | 85 | -- |
| `capture_long_edge` | 2400 px | a captured still and its page image are downscaled to it |
| `capture_jpeg_quality` | 85 | -- |
| `capture_window_before_seconds` | 20 | a capture's transcript window starts this long before it |
| `capture_window_after_seconds` | 10 | ...and ends this long after it |
