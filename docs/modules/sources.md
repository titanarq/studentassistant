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
