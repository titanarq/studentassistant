# ADR-0001: Thin phone, smart PC

Status: accepted (2026-09-24)

## Context
The student shows paper notes to a phone and talks. Handwriting must be legible to a vision
model, the conversation must be transcribed, and everything must be understood and stored.

## Decision
- The Android app is a **thin capture client**: pairing, session selection, camera preview,
  microphone streaming, still capture, a few buttons, live transcript display, offline spool.
  It holds no study logic.
- The phone streams **audio only** (16 kHz mono PCM16 over a WebSocket) and uploads **still
  photos** (CameraX full-resolution, a burst of 3) only when asked: a button, or a
  `capture_now` command the backend sends after detecting a voice command. **No video is
  streamed or stored.** The camera preview exists only on the phone screen.
- The Ubuntu PC runs the backend: STT, source processing, observer, editor, vault, web UI.
- Phone and PC talk over the home LAN only. The backend binds LAN interfaces, pairs a phone once
  via a QR code carrying a one-time code, and requires `Authorization: Bearer <token>` on every
  endpoint except `GET /api/health` and `POST /api/pair`. Tokens and API keys are never logged.
- Photo/voice alignment uses timestamps: the phone and backend agree a clock offset at
  WebSocket connect; every audio frame and capture carries the phone's capture time.

## Consequences
- High-resolution stills read handwriting far better than video frames and cost less.
- Automatic capture on page turn (would need frames) is deferred; if added, it runs on the phone.
- The backend can be developed and tested without a phone through `studentassistant replay`.
