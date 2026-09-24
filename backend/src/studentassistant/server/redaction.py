"""Keep bearer tokens and pairing codes out of every log record (ADR-0001).

`install_log_redaction` wraps the process's log record factory, so every record of every logger
-- uvicorn's access and error logs included, and whatever handler is attached later -- has its
message, its string arguments and its formatted traceback redacted when it is created. Redacted:
`Bearer <anything>`, `token=<anything>` (the WebSocket query parameter), anything shaped like one
of our tokens (`sa_...`) or pairing codes (`XXXX-XXXX`), and the JSON fields `token` and
`pairing_code`.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any

from studentassistant.server.devices import TOKEN_PREFIX
from studentassistant.server.pairing import CODE_ALPHABET, CODE_GROUP

REDACTED = "[REDACTED]"

_code_chars = f"[{CODE_ALPHABET}]"
_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[^\s\"',]+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)((?:^|[?&\s])token=)[^&\s\"']+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)(\"(?:token|pairing_code|code)\"\s*:\s*\")[^\"]*"), rf"\1{REDACTED}"),
    (re.compile(rf"{re.escape(TOKEN_PREFIX)}[A-Za-z0-9_-]{{16,}}"), REDACTED),
    (re.compile(rf"\b{_code_chars}{{{CODE_GROUP}}}-{_code_chars}{{{CODE_GROUP}}}\b"), REDACTED),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Redact `record` in place, keeping the shape of `args` (uvicorn formats them)."""
    if isinstance(record.msg, str):
        record.msg = redact(record.msg)
    if isinstance(record.args, tuple):
        record.args = tuple(_redact_value(arg) for arg in record.args)
    elif isinstance(record.args, dict):
        record.args = {key: _redact_value(value) for key, value in record.args.items()}
    if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
        # Formatters reuse `exc_text` when it is set, so the traceback is only ever seen redacted.
        record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
    return record


_installed = False


def install_log_redaction() -> None:
    """Make every future log record of the process pass through `redact_record` (idempotent)."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return redact_record(previous(*args, **kwargs))

    logging.setLogRecordFactory(factory)
    _installed = True
