"""Capture triage: cheap, deterministic, GPU-free checks that set a bad capture aside.

Every capture is triaged when it is stored (`store_capture`), before anything reads it. The checks
run on what `process_burst` made of the burst (OpenCV and NumPy only, no LLM):

- **blank**: the share of ink pixels on the page image (a pixel clearly darker than its
  neighbourhood, `ink_mask`, measured inside the central part of the image so a desk edge at the
  frame does not count) is below `[sources] triage_blank_max_ink`. Paper texture, a shadow or the
  faint grid of a squared notebook are not ink; a few handwritten lines are.
- **duplicate**: the 64-bit difference hash (`dhash`, on the page image's smoothed ink density)
  is within `triage_duplicate_max_distance` bits of a kept capture of the same topic and source
  kind (their stored `triage.metrics.dhash`), confirmed by the finer 256-bit hash (`dhash256`)
  within four times that. The newer capture is set aside as `duplicate` of the nearest one,
  unless it is `triage_duplicate_sharper_ratio` times sharper: then it is kept and the older one
  is set aside instead -- except when the older one is *protected*: the current notes link it
  (`../sources/<kind>/page-NNN.jpg`) or the student decided about it.
- **blurry**: the kept still's sharpness (the variance of the Laplacian at the 1200 px scale of
  `pick_sharpest`) is below `triage_min_sharpness`. A blank page is only `blank` (an empty sheet
  is never sharp).
- **partial**: a detected page with corners on two or more sides of the image, or ink along one
  side of the frame beyond `triage_partial_max_border_ink` (share of that side's band). A partial
  capture is `flagged` (kept, with the reason) unless `triage_partial_sets_aside`.

After a page is transcribed, `check_same_content` compares its normalised text (no case, accents,
Markdown or `[[?...]]` marks) with the kept pages of the topic and kind; a pair at least
`triage_same_content_min_similarity` alike sets aside the one with more `[[?` marks (ties: the less
sharp) as `same_content`, never a protected page.

The decision is stored in the page's sidecar as `triage: {status, reasons, duplicate_of, metrics,
decided_by, decided_at}` (plus `history`, `note` when there are). A capture set aside stays in the
vault: it is never transcribed (`transcriber.py`, `catchup.py`) nor given to the editor
(`editor.inputs`). The student can set one aside or restore it (`set_capture_triage`); sources
stored before triage existed read as `kept`, `decided_by: legacy` (`triage_status`).

`triage_capture` and everything above the vault section are pure; the rest reads and writes the
vault only through `studentassistant.vault` (blocking: call it from a worker thread).
"""

from __future__ import annotations

import contextlib
import difflib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.config import SourcesSettings
from studentassistant.sources.captures import (
    PAGE_SUFFIX,
    ProcessedBurst,
    decode_image,
    downscale,
    find_page,
)
from studentassistant.sources.captures import sharpness as still_sharpness
from studentassistant.vault import (
    GitSync,
    SourceError,
    SourceNotFoundError,
    Vault,
    list_sources,
    read_notes,
    read_source,
    topic_directory,
    update_page_meta,
)

CAPTURE_TRIAGED_KIND = "capture.triaged"
"""The persisted event of a triage decision (origin `observer`; `user` for a student decision)."""
TRIAGE_KEY = "triage"
"""The sidecar key holding a page's triage."""
CAPTURE_KINDS: tuple[str, ...] = ("notes", "book")
"""The source kinds whose pages are captures (and are triaged)."""

TriageStatus = Literal["kept", "flagged", "set_aside"]
TriageReason = Literal["blank", "duplicate", "blurry", "partial", "same_content"]
DecidedBy = Literal["auto", "student", "legacy"]
StudentDecision = Literal["set_aside", "restore"]

TRIAGE_REASONS: tuple[str, ...] = ("blank", "duplicate", "blurry", "partial", "same_content")
REASON_TEXT: dict[str, str] = {
    "blank": "página en blanco",
    "duplicate": "repetida",
    "blurry": "borrosa",
    "partial": "cortada",
    "same_content": "mismo contenido que otra",
}
"""Each reason in Spanish, for what the student reads."""

# Every image is analysed at this long edge (the scale of `captures.pick_sharpest`).
_ANALYSIS_LONG_EDGE = 1200
# A pixel is ink when it is this much darker (share) than the mean of its neighbourhood...
_INK_DARKNESS = 0.25
# ...a square of this many pixels (at the analysis scale).
_INK_NEIGHBOURHOOD = 41
# The ink ratio is measured inside the image minus this share of each side.
_INK_MARGIN = 0.08
# The frame's border bands are this share of the long edge wide.
_BORDER_BAND = 0.02
# A page corner this close to the image border (share of the long edge) is on it.
_CORNER_MARGIN = 0.01
_DHASH_SIZE = 8
# The finer hash that confirms a 64-bit match: 16x16 cells, its distance allowed 4x as many bits
# (two different pages of lined handwriting can come within 10 bits on 64, never on 256).
_FINE_DHASH_SIZE = 16
_FINE_DISTANCE_FACTOR = 4
_DHASH_SMOOTHING = 0.02
# Transcriptions with fewer words than this are never compared (an illegible page's `[[?]]`).
_MIN_COMPARED_WORDS = 5

_PAGE_NAME = re.compile(r"^page-(\d{3,})\.")
_UNCERTAIN = re.compile(r"\[\[\?([^\]]*)\]\]")
_NOTES_LINK = re.compile(r"\.\./(sources/[a-z]+/[^\s)#\"'>]+)")
_MARKDOWN = re.compile(r"[#*_`>|~\[\]()!\\=+\-–—→⇒]")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TriageResult(BaseModel):
    """One capture's triage, as its sidecar's `triage` block stores it.

    `replaces` (never stored) is the older capture a sharper duplicate displaces: `store_capture`
    sets that one aside as `duplicate` of this one.
    """

    model_config = ConfigDict(extra="ignore")

    status: TriageStatus = "kept"
    reasons: list[TriageReason] = Field(default_factory=list)
    duplicate_of: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    decided_by: DecidedBy = "auto"
    decided_at: datetime | None = None
    note: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    replaces: str | None = Field(default=None, exclude=True)

    @property
    def set_aside(self) -> bool:
        return self.status == "set_aside"

    def sidecar(self) -> dict[str, Any]:
        """The `triage` block written to the sidecar (JSON-compatible)."""
        block = self.model_dump(mode="json", exclude={"note", "history"})
        if self.note is not None:
            block["note"] = self.note
        if self.history:
            block["history"] = self.model_dump(mode="json")["history"]
        return block


LEGACY = TriageResult(decided_by="legacy")
"""What a source stored before triage existed reads as."""


@dataclass(frozen=True)
class TriageRecord:
    """What triage knows of another capture of the topic and kind.

    `source_id` is topic-relative (`sources/notes/page-003.jpg`); `protected` when the current
    notes link it or the student decided about it (never set aside automatically).
    """

    source_id: str
    status: TriageStatus
    dhash: str | None
    sharpness: float | None
    protected: bool = False
    dhash256: str | None = None


@dataclass(frozen=True)
class TriageChange:
    """A triage decision about a stored capture, as `capture.triaged` publishes it."""

    source_id: str
    source_path: str
    capture_id: str | None
    capture_session_id: str | None
    result: TriageResult


# -- metrics (pure) --------------------------------------------------------------------------------


def _gray(image: np.ndarray) -> np.ndarray:
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return downscale(gray, _ANALYSIS_LONG_EDGE)


def ink_mask(gray: np.ndarray) -> np.ndarray:
    """The ink pixels of a grayscale image: clearly darker than their neighbourhood's mean.

    Relative darkness (not a fixed grey level) keeps a shadowed stretch of paper, paper texture
    and the faint grid of a squared notebook out; a 2x2 opening removes single-pixel noise.
    """
    values = gray.astype(np.float32)
    mean = cv2.blur(values, (_INK_NEIGHBOURHOOD, _INK_NEIGHBOURHOOD))
    mask = (values < mean * (1 - _INK_DARKNESS)).astype(np.uint8)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)) > 0


def ink_ratio(gray: np.ndarray) -> float:
    """The share of ink pixels inside the image minus a margin on each side."""
    mask = ink_mask(gray)
    height, width = mask.shape
    my, mx = int(height * _INK_MARGIN), int(width * _INK_MARGIN)
    inner = mask[my : height - my, mx : width - mx]
    return float(inner.mean()) if inner.size else 0.0


def border_ink(gray: np.ndarray) -> float:
    """The largest share of ink in one of the four border bands of the frame."""
    mask = ink_mask(gray)
    height, width = mask.shape
    band = max(2, round(_BORDER_BAND * max(height, width)))
    bands = (mask[:band], mask[-band:], mask[:, :band], mask[:, -band:])
    return max(float(b.mean()) for b in bands)


def dhash(gray: np.ndarray, size: int = _DHASH_SIZE) -> str:
    """The difference hash of a grayscale page image (`size`² bits: 64 by default, 16 hex
    digits; `size=16` gives the finer 256-bit one).

    Computed on the page's ink density (`ink_mask`) rather than its grey levels: a notebook page
    is mostly paper, whose cells differ only by noise and lighting, so on grey levels their bits
    flip between two shots of one page; empty cells of ink compare equal (bit 0) every time, and
    the ink map ignores exposure and shadows. The map is smoothed first (a Gaussian of
    `_DHASH_SMOOTHING` of the long edge), so a small shift or rescale of the page moves density
    across cell borders gradually instead of flipping bits.
    """
    density = ink_mask(gray).astype(np.float32)
    sigma = max(1.0, _DHASH_SMOOTHING * max(gray.shape[:2]))
    density = cv2.GaussianBlur(density, (0, 0), sigma)
    small = cv2.resize(density, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{size * size // 4}x}"


def hamming(first: str, second: str) -> int:
    """The number of differing bits between two hex hashes."""
    return (int(first, 16) ^ int(second, 16)).bit_count()


def page_border_sides(corners: Sequence[Sequence[float]] | None, width: int, height: int) -> int:
    """How many sides of the image a detected page's corners touch (0 without a page)."""
    if not corners:
        return 0
    margin = max(2.0, _CORNER_MARGIN * max(width, height))
    sides: set[str] = set()
    for x, y in corners:
        if x < margin:
            sides.add("left")
        if x >= width - margin:
            sides.add("right")
        if y < margin:
            sides.add("top")
        if y >= height - margin:
            sides.add("bottom")
    return len(sides)


def capture_metrics(processed: ProcessedBurst) -> dict[str, Any]:
    """The measurements triage decides on: `ink_ratio` and `dhash` of the page image, the frame's
    `border_ink`, the kept still's `sharpness`, `page_detected` and `page_border_sides`."""
    page = decode_image(processed.page)
    still = decode_image(processed.still)
    page_gray = _gray(page) if page is not None else None
    still_gray = _gray(still) if still is not None else page_gray
    score = processed.sharpness[processed.selected] if processed.sharpness else None
    return {
        "ink_ratio": round(ink_ratio(page_gray), 6) if page_gray is not None else 0.0,
        "border_ink": round(border_ink(still_gray), 6) if still_gray is not None else 0.0,
        "sharpness": round(score, 3) if score is not None else None,
        "dhash": dhash(page_gray) if page_gray is not None else None,
        "dhash256": dhash(page_gray, _FINE_DHASH_SIZE) if page_gray is not None else None,
        "page_detected": processed.page_detected,
        "page_border_sides": page_border_sides(
            processed.page_corners, processed.width_px, processed.height_px
        ),
    }


def status_for(reasons: Sequence[str], settings: SourcesSettings) -> TriageStatus:
    """`set_aside` when a reason sets aside, `flagged` for `partial` alone (by default), or kept."""
    if not reasons:
        return "kept"
    if set(reasons) == {"partial"} and not settings.triage_partial_sets_aside:
        return "flagged"
    return "set_aside"


def triage_capture(
    processed: ProcessedBurst,
    others: Sequence[TriageRecord],
    settings: SourcesSettings,
    *,
    now: datetime | None = None,
) -> TriageResult:
    """The triage of a new capture against the topic's other captures of its kind (pure).

    See the module docstring for the checks. `others` are the stored captures of the same topic
    and kind; only those not set aside and with a stored `dhash` are compared.
    """
    metrics = capture_metrics(processed)
    return decide(metrics, others, settings, now=now)


def decide(
    metrics: Mapping[str, Any],
    others: Sequence[TriageRecord],
    settings: SourcesSettings,
    *,
    now: datetime | None = None,
) -> TriageResult:
    """The decision `triage_capture` takes on already computed `metrics` (pure)."""
    measured = dict(metrics)
    reasons: list[TriageReason] = []
    duplicate_of: str | None = None
    replaces: str | None = None
    score = measured.get("sharpness")
    if measured.get("ink_ratio", 0.0) < settings.triage_blank_max_ink:
        reasons.append("blank")
    else:
        blurry = score is not None and score < settings.triage_min_sharpness
        nearest = _nearest(measured.get("dhash"), measured.get("dhash256"), settings, others)
        if nearest is not None:
            record, distance, fine = nearest
            measured["duplicate_distance"] = distance
            if fine is not None:
                measured["duplicate_distance_256"] = fine
            if _matches(distance, fine, settings):
                sharper = (
                    not blurry
                    and score is not None
                    and record.sharpness is not None
                    and score >= record.sharpness * settings.triage_duplicate_sharper_ratio
                )
                if sharper and not record.protected:
                    replaces = record.source_id
                else:
                    reasons.append("duplicate")
                    duplicate_of = record.source_id
        if blurry:
            reasons.append("blurry")
        border_sides = int(measured.get("page_border_sides") or 0)
        if (
            border_sides >= 2
            or measured.get("border_ink", 0.0) > settings.triage_partial_max_border_ink
        ):
            reasons.append("partial")
    return TriageResult(
        status=status_for(reasons, settings),
        reasons=reasons,
        duplicate_of=duplicate_of,
        metrics=measured,
        decided_by="auto",
        decided_at=now or _utc_now(),
        replaces=replaces,
    )


def _matches(distance: int, fine: int | None, settings: SourcesSettings) -> bool:
    """A 64-bit distance within the threshold, confirmed by the 256-bit one when both have it."""
    limit = settings.triage_duplicate_max_distance
    return distance <= limit and (fine is None or fine <= limit * _FINE_DISTANCE_FACTOR)


def _nearest(
    hash_: str | None,
    fine_hash: str | None,
    settings: SourcesSettings,
    others: Sequence[TriageRecord],
) -> tuple[TriageRecord, int, int | None] | None:
    """The comparable record nearest to `hash_`, with its 64-bit and 256-bit distances: a
    matching one first, then the smallest distance, the latest among equals."""
    if not hash_:
        return None
    best: tuple[TriageRecord, int, int | None] | None = None
    best_key: tuple[bool, int] | None = None
    for record in others:
        if record.status == "set_aside" or not record.dhash:
            continue
        try:
            distance = hamming(hash_, record.dhash)
            fine = hamming(fine_hash, record.dhash256) if fine_hash and record.dhash256 else None
        except ValueError:
            continue
        key = (not _matches(distance, fine, settings), distance)
        if best_key is None or key <= best_key:
            best, best_key = (record, distance, fine), key
    return best


def ambiguous_checks(result: TriageResult, settings: SourcesSettings) -> list[str]:
    """The checks (`blank`, `blurry`, `partial`) whose metric is within `triage_llm_margin`
    (relative) of its threshold: those the optional Sonnet stage may decide instead."""
    metrics = result.metrics
    margin = settings.triage_llm_margin
    checks: list[str] = []

    def near(value: Any, threshold: float) -> bool:
        return isinstance(value, int | float) and abs(value - threshold) <= margin * threshold

    if near(metrics.get("ink_ratio"), settings.triage_blank_max_ink):
        checks.append("blank")
    sharpness = metrics.get("sharpness")
    if "blank" not in result.reasons and near(sharpness, settings.triage_min_sharpness):
        checks.append("blurry")
    if (
        "blank" not in result.reasons
        and int(metrics.get("page_border_sides") or 0) < 2
        and near(metrics.get("border_ink"), settings.triage_partial_max_border_ink)
    ):
        checks.append("partial")
    return checks


def with_verdict(
    result: TriageResult,
    verdict: Mapping[str, bool],
    settings: SourcesSettings,
    checks: Sequence[str],
) -> TriageResult:
    """`result` with each of `checks` (blank, blurry, partial) replaced by `verdict[check]`."""
    reasons: list[TriageReason] = [r for r in result.reasons if r not in checks]
    for check in checks:
        if verdict.get(check):
            reasons.append(check)  # type: ignore[arg-type]
    if "blank" in reasons:  # a blank page is only blank
        reasons = ["blank"]
    ordered = [r for r in TRIAGE_REASONS if r in reasons]
    metrics = dict(result.metrics)
    metrics["llm_verdict"] = {check: bool(verdict.get(check)) for check in checks}
    return result.model_copy(
        update={
            "reasons": ordered,
            "status": status_for(ordered, settings),
            "duplicate_of": result.duplicate_of if "duplicate" in ordered else None,
            "replaces": None if "blank" in ordered else result.replaces,
            "metrics": metrics,
        }
    )


# -- text comparison (pure) ------------------------------------------------------------------------


def normalise_text(text: str) -> list[str]:
    """The words of a transcription with no case, accents, Markdown nor `[[?...]]` marks."""
    text = _UNCERTAIN.sub(lambda match: f" {match.group(1)} ", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    plain = _MARKDOWN.sub(" ", plain)
    plain = re.sub(r"[^\w\s]", " ", plain)
    return plain.split()


def text_similarity(first: str, second: str) -> float:
    """How alike two transcriptions are (0-1, word sequences); 0 when either is nearly empty."""
    a, b = normalise_text(first), normalise_text(second)
    if len(a) < _MIN_COMPARED_WORDS or len(b) < _MIN_COMPARED_WORDS:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def uncertain_marks(text: str) -> int:
    return text.count("[[?")


# -- vault -----------------------------------------------------------------------------------------


def page_number(source_id: str) -> int | None:
    match = _PAGE_NAME.match(PurePosixPath(source_id).name)
    return int(match.group(1)) if match else None


def _prefix(vault: Vault, subject_slug: str, topic_slug: str) -> str:
    return topic_directory(vault, subject_slug, topic_slug).relative_to(vault.path).as_posix()


def topic_source_id(source_path: str) -> str:
    """`subjects/<s>/topics/<t>/sources/<kind>/<file>` -> `sources/<kind>/<file>` (unchanged when
    already topic-relative)."""
    parts = PurePosixPath(source_path).parts
    if "sources" in parts:
        return PurePosixPath(*parts[parts.index("sources") :]).as_posix()
    return source_path


def _vault_path(vault: Vault, subject_slug: str, topic_slug: str, source_id: str) -> str:
    return f"{_prefix(vault, subject_slug, topic_slug)}/{topic_source_id(source_id)}"


def triage_of(meta: Mapping[str, Any] | None) -> TriageResult:
    """A sidecar's triage; `LEGACY` (kept) when it has none or it is not readable."""
    block = (meta or {}).get(TRIAGE_KEY)
    if not isinstance(block, Mapping):
        return LEGACY
    try:
        return TriageResult.model_validate(block)
    except ValidationError:
        return LEGACY


def triage_status(vault: Vault, subject_slug: str, topic_slug: str) -> dict[str, TriageResult]:
    """Every stored source of the topic (topic-relative id) with its triage; a source with no
    `triage` block (stored before triage, or not a capture) reads as kept, `decided_by: legacy`.

    Raises what `list_sources` raises.
    """
    prefix = _prefix(vault, subject_slug, topic_slug)
    return {
        source.path.removeprefix(prefix + "/"): triage_of(source.meta)
        for source in list_sources(vault, subject_slug, topic_slug)
    }


def set_aside_ids(vault: Vault, subject_slug: str, topic_slug: str) -> set[str]:
    """The topic-relative ids of the topic's set-aside sources."""
    return {
        source_id
        for source_id, result in triage_status(vault, subject_slug, topic_slug).items()
        if result.set_aside
    }


def is_set_aside(vault: Vault, source_path: str) -> bool:
    """Whether the stored page at `source_path` (vault-relative) is set aside; `False` when it
    cannot be read (the caller's own error handling decides then)."""
    try:
        return triage_of(read_source(vault, source_path).meta).set_aside
    except (SourceError, OSError):
        return False


def linked_sources(vault: Vault, subject_slug: str, topic_slug: str) -> set[str]:
    """The topic-relative source ids the current `notes/apuntes.md` links (`../sources/...`)."""
    text = read_notes(vault, subject_slug, topic_slug) or ""
    return {match.group(1) for match in _NOTES_LINK.finditer(text)}


def triage_records(
    vault: Vault, subject_slug: str, topic_slug: str, kind: str
) -> list[TriageRecord]:
    """The topic's stored captures of `kind` as triage compares them, oldest first."""
    prefix = _prefix(vault, subject_slug, topic_slug)
    linked = linked_sources(vault, subject_slug, topic_slug)
    records: list[TriageRecord] = []
    for source in list_sources(vault, subject_slug, topic_slug):
        if source.kind != kind:
            continue
        source_id = source.path.removeprefix(prefix + "/")
        result = triage_of(source.meta)
        records.append(_record(source_id, result, source.meta, linked))
    return records


def _record(
    source_id: str, result: TriageResult, meta: Mapping[str, Any] | None, linked: set[str]
) -> TriageRecord:
    score = result.metrics.get("sharpness")
    if not isinstance(score, int | float):
        score = _selected_sharpness(meta)
    hash_ = result.metrics.get("dhash")
    fine = result.metrics.get("dhash256")
    return TriageRecord(
        source_id=source_id,
        status=result.status,
        dhash=hash_ if isinstance(hash_, str) else None,
        dhash256=fine if isinstance(fine, str) else None,
        sharpness=float(score) if isinstance(score, int | float) else None,
        protected=source_id in linked or result.decided_by == "student",
    )


def _selected_sharpness(meta: Mapping[str, Any] | None) -> float | None:
    """The kept still's sharpness from a capture sidecar (`sharpness[selected_image - 1]`)."""
    meta = meta or {}
    scores, selected = meta.get("sharpness"), meta.get("selected_image")
    if isinstance(scores, list) and isinstance(selected, int) and 1 <= selected <= len(scores):
        value = scores[selected - 1]
        return float(value) if isinstance(value, int | float) else None
    return None


def prepare_triage(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    kind: str,
    processed: ProcessedBurst,
    settings: SourcesSettings,
    *,
    now: datetime | None = None,
) -> TriageResult:
    """`triage_capture` of a burst about to be stored, against the topic's captures of `kind`."""
    others = triage_records(vault, subject_slug, topic_slug, kind)
    return triage_capture(processed, others, settings, now=now)


def _change(
    vault: Vault, subject_slug: str, topic_slug: str, source_id: str, result: TriageResult
) -> TriageChange:
    source_path = _vault_path(vault, subject_slug, topic_slug, source_id)
    meta: Mapping[str, Any] = {}
    with contextlib.suppress(SourceError, OSError):
        meta = read_source(vault, source_path).meta or {}
    capture_id, session = meta.get("capture_id"), meta.get("session")
    return TriageChange(
        source_id=topic_source_id(source_id),
        source_path=source_path,
        capture_id=capture_id if isinstance(capture_id, str) else None,
        capture_session_id=session if isinstance(session, str) else None,
        result=result,
    )


def _write(
    vault: Vault, subject_slug: str, topic_slug: str, source_id: str, result: TriageResult
) -> TriageChange:
    source_path = _vault_path(vault, subject_slug, topic_slug, source_id)
    update_page_meta(vault, source_path, {TRIAGE_KEY: result.sidecar()})
    return _change(vault, subject_slug, topic_slug, source_id, result)


def _history_entry(result: TriageResult) -> dict[str, Any]:
    entry = result.model_dump(mode="json", include={"status", "reasons", "duplicate_of"})
    entry["decided_by"] = result.decided_by
    entry["decided_at"] = result.model_dump(mode="json")["decided_at"]
    if result.note is not None:
        entry["note"] = result.note
    return entry


def _auto_set_aside(
    current: TriageResult,
    reason: TriageReason,
    duplicate_of: str,
    now: datetime,
) -> TriageResult:
    return current.model_copy(
        update={
            "status": "set_aside",
            "reasons": [reason],
            "duplicate_of": duplicate_of,
            "decided_by": "auto",
            "decided_at": now,
            "history": [*current.history, _history_entry(current)],
            "replaces": None,
        }
    )


def apply_swap(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    new_source_id: str,
    result: TriageResult,
    *,
    now: datetime | None = None,
) -> TriageChange | None:
    """Set aside `result.replaces` (an older capture a sharper duplicate displaces) as a
    `duplicate` of `new_source_id`; `None` when there is nothing to set aside (it is protected
    now, or set aside already)."""
    if result.replaces is None:
        return None
    older = topic_source_id(result.replaces)
    path = _vault_path(vault, subject_slug, topic_slug, older)
    try:
        meta = read_source(vault, path).meta
    except SourceNotFoundError:
        return None
    current = triage_of(meta)
    if current.set_aside or current.decided_by == "student":
        return None
    if older in linked_sources(vault, subject_slug, topic_slug):
        return None
    moment = now or _utc_now()
    changed = _auto_set_aside(current, "duplicate", topic_source_id(new_source_id), moment)
    return _write(vault, subject_slug, topic_slug, older, changed)


def set_capture_triage(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    source_id: str,
    decision: StudentDecision,
    *,
    reason: str | None = None,
    sync: GitSync | None,
    now: datetime | None = None,
) -> TriageResult:
    """The student sets a capture aside, or restores it (`kept`, reasons cleared), and commits.

    `source_id` is topic-relative (`sources/notes/page-009.jpg`) or vault-relative. `reason` is
    a triage reason (kept in `reasons`) or the student's own words (kept as `note`). The previous
    decision goes to `triage.history`; `decided_by` is `student`. The commit message is Spanish
    (`Página 9 de <s>/<t> apartada` / `recuperada`). The caller publishes `capture.triaged`
    (origin `user`); a restored page with no transcription is owed again (the transcriber takes it
    on that event, or the next catch-up does).

    Raises:
        ValueError: `decision` is not `set_aside` or `restore`.
        SourcePathError, SourceNotFoundError: not a stored page of the topic.
    """
    if decision not in ("set_aside", "restore"):
        raise ValueError(f"{decision!r} is not a triage decision (set_aside or restore)")
    topic_id = topic_source_id(source_id)
    path = _vault_path(vault, subject_slug, topic_slug, topic_id)
    current = triage_of(read_source(vault, path).meta)
    reasons: list[TriageReason] = []
    note: str | None = None
    if decision == "set_aside" and reason is not None:
        if reason in TRIAGE_REASONS:
            reasons = [reason]  # type: ignore[list-item]
        else:
            note = reason.strip() or None
    history = list(current.history)
    if current.decided_by != "legacy":
        history.append(_history_entry(current))
    result = TriageResult(
        status="set_aside" if decision == "set_aside" else "kept",
        reasons=reasons,
        duplicate_of=None,
        metrics=current.metrics,
        decided_by="student",
        decided_at=now or _utc_now(),
        note=note,
        history=history,
    )
    update_page_meta(vault, path, {TRIAGE_KEY: result.sidecar()})
    if sync is not None:
        number = page_number(topic_id)
        label = f"Página {number}" if number is not None else f"La fuente {topic_id}"
        done = "apartada" if decision == "set_aside" else "recuperada"
        sync.note_change()
        sync.checkpoint(f"{label} de {subject_slug}/{topic_slug} {done}")
    return result


def check_same_content(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    source_path: str,
    text: str,
    settings: SourcesSettings,
    *,
    now: datetime | None = None,
) -> list[TriageChange]:
    """After `source_path`'s transcription `text` is stored: set aside the worse of each pair of
    kept pages of the topic and kind whose transcriptions are at least
    `triage_same_content_min_similarity` alike (see the module docstring). Returns the changes
    written (the new page's own, when it is the one set aside, stops the comparison)."""
    if not settings.triage_enabled:
        return []
    new_id = topic_source_id(source_path)
    kind = PurePosixPath(new_id).parent.name
    if kind not in CAPTURE_KINDS:
        return []
    moment = now or _utc_now()
    prefix = _prefix(vault, subject_slug, topic_slug)
    linked = linked_sources(vault, subject_slug, topic_slug)
    sources = {
        source.path.removeprefix(prefix + "/"): source
        for source in list_sources(vault, subject_slug, topic_slug)
        if source.kind == kind
    }
    new_source = sources.get(new_id)
    if new_source is None:
        return []
    new_triage = triage_of(new_source.meta)
    if new_triage.set_aside:
        return []
    new_record = _record(new_id, new_triage, new_source.meta, linked)
    changes: list[TriageChange] = []
    for other_id, other in sources.items():
        if other_id == new_id:
            continue
        other_triage = triage_of(other.meta)
        if other_triage.set_aside:
            continue
        other_text = _transcription_text(vault, other.path)
        if other_text is None:
            continue
        similarity = text_similarity(text, other_text)
        if similarity < settings.triage_same_content_min_similarity:
            continue
        other_record = _record(other_id, other_triage, other.meta, linked)
        loser = _worse(
            (new_record, uncertain_marks(text)), (other_record, uncertain_marks(other_text))
        )
        if loser is None:
            continue
        loser_id = loser.source_id
        winner_id = other_id if loser_id == new_id else new_id
        loser_triage = new_triage if loser_id == new_id else other_triage
        changed = _auto_set_aside(loser_triage, "same_content", winner_id, moment)
        changed.metrics["same_content_similarity"] = round(similarity, 4)
        changes.append(_write(vault, subject_slug, topic_slug, loser_id, changed))
        if loser_id == new_id:
            break
    return changes


def _worse(
    first: tuple[TriageRecord, int], second: tuple[TriageRecord, int]
) -> TriageRecord | None:
    """Of two alike pages, the one to set aside: more `[[?` marks, ties to the less sharp (then
    the newer); never a protected one (`None` when both are)."""
    (a, a_marks), (b, b_marks) = first, second
    if a_marks != b_marks:
        order = (a, b) if a_marks > b_marks else (b, a)
    else:
        a_sharp = a.sharpness if a.sharpness is not None else -1.0
        b_sharp = b.sharpness if b.sharpness is not None else -1.0
        order = (a, b) if a_sharp <= b_sharp else (b, a)
    for candidate in order:
        if not candidate.protected:
            return candidate
    return None


def _transcription_text(vault: Vault, source_path: str) -> str | None:
    path = PurePosixPath(source_path)
    md = path.with_name(path.name.split(".", 1)[0] + ".md").as_posix()
    try:
        return read_source(vault, md).content.decode("utf-8", errors="replace")
    except (SourceError, OSError):
        return None


def triaged_payload(change: TriageChange) -> dict[str, Any]:
    """The payload of `capture.triaged` for one decision."""
    result = change.result
    payload: dict[str, Any] = {
        "capture_id": change.capture_id,
        "source_path": change.source_path,
        "source_id": change.source_id,
        "status": result.status,
        "reasons": list(result.reasons),
        "duplicate_of": result.duplicate_of,
        "decided_by": result.decided_by,
    }
    if change.capture_session_id is not None:
        payload["capture_session_id"] = change.capture_session_id
    return payload


def change_for(
    vault: Vault, subject_slug: str, topic_slug: str, source_id: str, result: TriageResult
) -> TriageChange:
    """The `TriageChange` of a decision already written (to publish it)."""
    return _change(vault, subject_slug, topic_slug, source_id, result)


# -- retro-triage (the `triage` CLI) ---------------------------------------------------------------


@dataclass(frozen=True)
class RetroTriage:
    """One capture as `retro_triage` sees it: its stored triage (`None` when it has none) and what
    the current thresholds decide; `written` when `apply` stored that decision."""

    source_id: str
    kind: str
    current: TriageResult | None
    decided: TriageResult
    written: bool = False


def stored_burst(vault: Vault, source_path: str) -> ProcessedBurst | None:
    """A stored capture read back as `process_burst` made it (still, page image, sharpness, page
    corners found again on the still); `None` when its images are not there."""
    try:
        stored = read_source(vault, source_path)
        stem = PurePosixPath(source_path).name.split(".", 1)[0]
        page_path = PurePosixPath(source_path).with_name(f"{stem}.{PAGE_SUFFIX}").as_posix()
        try:
            page = read_source(vault, page_path).content
        except SourceNotFoundError:
            page = stored.content
    except (SourceError, OSError):
        return None
    still = decode_image(stored.content)
    if still is None:
        return None
    meta = stored.meta or {}
    scores = meta.get("sharpness")
    selected = meta.get("selected_image")
    sharpness: tuple[float | None, ...]
    index = 0
    if isinstance(scores, list) and scores:
        sharpness = tuple(float(s) if isinstance(s, int | float) else None for s in scores)
        if isinstance(selected, int) and 1 <= selected <= len(scores):
            index = selected - 1
    else:
        sharpness = (still_sharpness(still),)
    detected = bool(meta.get("page_detected", False))
    corners = find_page(still) if detected else None
    return ProcessedBurst(
        selected=index,
        sharpness=sharpness,
        still=stored.content,
        page=page,
        page_detected=detected,
        width_px=int(still.shape[1]),
        height_px=int(still.shape[0]),
        page_corners=None
        if corners is None
        else tuple((float(x), float(y)) for x, y in corners.tolist()),
    )


def retro_triage(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    settings: SourcesSettings,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> list[RetroTriage]:
    """Triage every stored capture of the topic, oldest first, as if it arrived now in that order.

    Each capture is compared with the ones before it (their stored triage, or what this pass
    decided for them). Nothing is written unless `apply`: then each capture without a `triage`
    block gets the decision (and a displaced older duplicate is set aside, `apply_swap`); captures
    with one are left as they are. Committing is the caller's.
    """
    moment = now or _utc_now()
    prefix = _prefix(vault, subject_slug, topic_slug)
    linked = linked_sources(vault, subject_slug, topic_slug)
    rows: list[RetroTriage] = []
    for kind in CAPTURE_KINDS:
        seen: list[TriageRecord] = []
        for source in list_sources(vault, subject_slug, topic_slug):
            if source.kind != kind:
                continue
            source_id = source.path.removeprefix(prefix + "/")
            block = (source.meta or {}).get(TRIAGE_KEY)
            current = triage_of(source.meta) if isinstance(block, Mapping) else None
            processed = stored_burst(vault, source.path)
            if processed is None:
                continue
            decided = triage_capture(processed, seen, settings, now=moment)
            written = False
            if apply and current is None:
                update_page_meta(vault, source.path, {TRIAGE_KEY: decided.sidecar()})
                written = True
                if decided.replaces is not None:
                    swapped = apply_swap(
                        vault, subject_slug, topic_slug, source_id, decided, now=moment
                    )
                    if swapped is not None:
                        seen = [
                            _record(r.source_id, swapped.result, None, linked)
                            if r.source_id == swapped.source_id
                            else r
                            for r in seen
                        ]
            effective = current if current is not None else decided
            if effective.decided_by == "legacy":
                effective = decided
            seen.append(_record(source_id, effective, source.meta, linked))
            rows.append(RetroTriage(source_id, kind, current, decided, written))
    return rows


_STATUS_TEXT = {"kept": "se queda", "flagged": "se queda (avisada)", "set_aside": "apartada"}


def describe(result: TriageResult) -> str:
    """A triage decision in Spanish: `apartada (repetida de sources/notes/page-001.jpg)`."""
    text = _STATUS_TEXT.get(result.status, result.status)
    reasons = [REASON_TEXT.get(reason, reason) for reason in result.reasons]
    if result.duplicate_of:
        reasons = [
            f"{reason} de {result.duplicate_of}"
            if code in ("duplicate", "same_content")
            else reason
            for code, reason in zip(result.reasons, reasons, strict=True)
        ]
    if result.replaces:
        reasons.append(f"más nítida que {result.replaces}, que se aparta")
    return f"{text} ({'; '.join(reasons)})" if reasons else text


def format_retro(row: RetroTriage) -> str:
    """One line of the `triage` CLI: the capture, its metrics and the decisions."""
    metrics = row.decided.metrics
    sharp = metrics.get("sharpness")
    parts = [
        f"tinta={metrics.get('ink_ratio', 0):.4f}",
        f"nitidez={sharp:.1f}" if isinstance(sharp, int | float) else "nitidez=?",
        f"borde={metrics.get('border_ink', 0):.4f}",
        f"dhash={metrics.get('dhash')}",
    ]
    if "duplicate_distance" in metrics:
        parts.append(f"distancia={metrics['duplicate_distance']}")
    line = f"{row.source_id}: {' '.join(parts)} -> {describe(row.decided)}"
    if row.current is not None:
        line += f" [guardado: {describe(row.current)}, {row.current.decided_by}]"
    elif row.written:
        line += " [escrito]"
    return line


__all__ = [
    "CAPTURE_KINDS",
    "CAPTURE_TRIAGED_KIND",
    "LEGACY",
    "REASON_TEXT",
    "TRIAGE_KEY",
    "TRIAGE_REASONS",
    "RetroTriage",
    "TriageChange",
    "TriageReason",
    "TriageRecord",
    "TriageResult",
    "TriageStatus",
    "ambiguous_checks",
    "apply_swap",
    "border_ink",
    "capture_metrics",
    "change_for",
    "check_same_content",
    "decide",
    "describe",
    "format_retro",
    "dhash",
    "hamming",
    "ink_mask",
    "ink_ratio",
    "is_set_aside",
    "linked_sources",
    "normalise_text",
    "page_border_sides",
    "page_number",
    "prepare_triage",
    "retro_triage",
    "set_aside_ids",
    "set_capture_triage",
    "status_for",
    "stored_burst",
    "text_similarity",
    "topic_source_id",
    "triage_capture",
    "triage_of",
    "triage_records",
    "triage_status",
    "triaged_payload",
    "with_verdict",
]
