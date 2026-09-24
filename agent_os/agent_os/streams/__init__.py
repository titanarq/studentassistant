"""Backend event-stream parsers, registered by name (#514).

Adding a CLI whose events are a NEW shape is one module here and one line in `STREAM_PARSERS`;
a CLI that emits a shape already registered only names that parser. Every reader in the mechanism
goes through `get_stream_parser`, so nothing outside this package knows which shape it is reading.
"""

from __future__ import annotations

from agent_os.streams.claude_jsonl import ClaudeJsonlStreamParser
from agent_os.streams.interface import (
    QuotaStatus,
    ResultUsage,
    StreamParser,
    StreamQuotaVerdict,
    UsageSummary,
)
from agent_os.streams.qwen_jsonl import QwenJsonlStreamParser

STREAM_PARSERS: dict[str, StreamParser] = {
    parser.name: parser for parser in (ClaudeJsonlStreamParser(), QwenJsonlStreamParser())
}

# What a reader that does not know which backend wrote a log parses it with -- a planner run's log,
# the `usage-report` / `cumulative-*` / `quota-status` CLI. Both registered shapes are read the
# same way today, so this is not a guess about the backend.
DEFAULT_STREAM_PARSER = ClaudeJsonlStreamParser.name


# The detectors a backend's `quota:` entry may name (`project.backends`, #514): whether an
# `exhausted` verdict read off that backend's stream CUTS its run. The verdict itself is always the
# stream parser's own `quota_verdict`, recorded for every backend alike, because the launch gate of
# a role reads it back whatever the backend is (#425); what differs per backend is only whether the
# guard acts on it. `claude_rate_limit` is the one detector that exists today -- a stream that
# carries a rejected `rate_limit_event`, or a refused `result` with `api_error_status == 429` (the
# 2026-09-14 quota ADR, narrowed to the 429 shape by #530) -- and `none` is a backend whose stream
# carries no quota signal the guard should cut on.
QUOTA_DETECTOR_NONE = "none"
QUOTA_DETECTORS: tuple[str, ...] = ("claude_rate_limit", QUOTA_DETECTOR_NONE)


class UnknownStreamParserError(ValueError):
    pass


class UnknownQuotaDetectorError(ValueError):
    pass


def check_quota_detector(name: str) -> str:
    if name not in QUOTA_DETECTORS:
        known = ", ".join(QUOTA_DETECTORS)
        raise UnknownQuotaDetectorError(
            f"unknown quota detector {name!r} -- registered detectors: {known}"
        )
    return name


def get_stream_parser(name: str) -> StreamParser:
    try:
        return STREAM_PARSERS[name]
    except KeyError:
        known = ", ".join(sorted(STREAM_PARSERS))
        raise UnknownStreamParserError(
            f"unknown stream parser {name!r} -- registered parsers: {known}"
        ) from None


__all__ = [
    "DEFAULT_STREAM_PARSER",
    "QUOTA_DETECTORS",
    "QUOTA_DETECTOR_NONE",
    "STREAM_PARSERS",
    "QuotaStatus",
    "ResultUsage",
    "StreamParser",
    "StreamQuotaVerdict",
    "UnknownQuotaDetectorError",
    "UnknownStreamParserError",
    "UsageSummary",
    "check_quota_detector",
    "get_stream_parser",
]
