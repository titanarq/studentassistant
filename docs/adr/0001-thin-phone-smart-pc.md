# ADR-0001: Thin capture clients, smart PC

Status: accepted (2026-09-24)

## Context
The student shows paper notes to a phone and talks. Handwriting must be legible to a vision
model, the conversation must be transcribed, and everything must be understood and stored.

## Decision
- Capture happens in **thin capture clients** that hold no study logic: first a **web capture
  page** (`/capture`, served by the backend) using the laptop's camera and microphone, which is
  the development client and the one milestone M1 is measured with; the **Android app** is built
  in parallel over the same protocol. Both do: session selection, camera preview, transcription
  or audio streaming (ADR-0008), still capture, a few buttons, live transcript display.
- A client sends **timed transcript segments** (default, client-side STT) or, when the backend
  asks for server-side STT, **audio** (16 kHz mono PCM16) over a WebSocket, and uploads **still
  photos** (full resolution, a burst of 3) only when asked: a button, or a `capture_now` command
  the backend sends after detecting a voice command. **No video is streamed or stored.** The
  camera preview exists only on the client's screen.
- A session is about exactly **one topic of one subject**, fixed when it starts.
- The Ubuntu PC runs the backend: STT, source processing, observer, editor, vault, web UI.
- Clients and PC talk over localhost or the home LAN only. The backend binds LAN interfaces, pairs a phone once
  via a QR code carrying a one-time code, and requires `Authorization: Bearer <token>` on every
  endpoint except `GET /api/health` and `POST /api/pair`. Tokens and API keys are never logged.
- Photo/voice alignment uses timestamps: the phone and backend agree a clock offset at
  WebSocket connect; every transcript segment, audio frame and capture carries the client's time.

## Consequences
- High-resolution stills read handwriting far better than video frames and cost less.
- Automatic capture on page turn (would need frames) is deferred; if added, it runs on the client.
- The backend can be developed and tested without a phone through `studentassistant replay`.
