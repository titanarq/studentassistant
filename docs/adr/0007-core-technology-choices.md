# ADR-0007: Core technology choices

Status: accepted (2026-09-24)

| Area | Choice |
|---|---|
| Backend | Python 3.12, `uv`, FastAPI + uvicorn, Pydantic v2, pydantic-settings, typer CLI, pytest, ruff |
| STT | faster-whisper (CTranslate2), default model `large-v3-turbo`, CUDA `int8_float16` on the PC's RTX 2060 (6 GB), CPU fallback; Silero VAD (bundled with faster-whisper); Spanish |
| Images | Pillow + OpenCV (headless) for sharpness, crop, deskew |
| PDF | PyMuPDF for page rendering/text; Claude native PDF input for the editor |
| LLM | `anthropic` Python SDK (ADR-0004); server tools `web_search_20260209` / `web_fetch_20260209` for web search |
| Storage | git CLI via subprocess on the vault (ADR-0002); SQLite (stdlib, FTS5) as derived index |
| Web | React + Vite + TypeScript, vitest; built assets served by FastAPI |
| Android | Kotlin, JDK 17, Compose Material 3, CameraX, AudioRecord, OkHttp WebSocket, kotlinx.serialization, QR scanning (ZXing embedded, no Play Services dependency), DataStore; `minSdk` 28, `targetSdk` 35 |
| Generators | genanki (Anki `.apkg`), Marp CLI (slides -> PDF/PPTX), Markdown -> PDF for printable exams |
| CI | GitHub Actions: backend, web, android jobs with path filters |
