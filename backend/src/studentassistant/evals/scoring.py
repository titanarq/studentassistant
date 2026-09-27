"""The eval rubric: deterministic scores of one replayed session against the student's reference.

Nothing here calls Claude or reads the disk, so a score is reproducible from the files a run
kept. Every score is in [0, 1], higher is better; the rubric is documented in
`docs/modules/infra.md` ("Evals").

Text is compared after `normalize`: the page transcriber's unreadable-word marks (`[[?x]]`)
become their word, footnote references, Markdown markup and punctuation go, accents are folded,
everything is lower-cased and runs of whitespace collapse. *Content words* are the normalized
words of three letters or more that are not Spanish stop words (numbers always count).

- Page transcription (`score_page`): character accuracy `1 - CER` and word accuracy `1 - WER`,
  Levenshtein distance over the reference's length, floored at 0.
- Observer sections (`score_sections`): pairwise agreement (the Rand index) between the reference
  sections and the observer's assignments over the reference's segments -- every pair of
  segments counts as agreeing when both put the pair together or both keep it apart, an
  unassigned segment being in no section -- plus coverage, the share of segments assigned at all.
- Editor fidelity (`score_notes`): the notes are cut into units (list items and sentences, no
  headings or footnote definitions). *Kept* is the share of reference units covered by one
  generated unit (at least `COVERED_SHARE` of the reference unit's content words in it): content
  dropped lowers it. *Supported* is the share of generated units whose content words are at least
  `SUPPORTED_SHARE` in the session's own material (transcript, page transcriptions) or the
  reference notes: content from nowhere lowers it.
- Request detection (`score_requests`): a detected request matches a reference request when both
  have the same kind and share at least one segment (each matched at most once, in order);
  precision, recall and F1 overall and per kind, plus the missed and spurious requests.
- Capture triage (`score_triage`): over the captures of the reference, "set aside" is the
  positive class -- precision, recall and F1 of the captures the run set aside -- and, per reason
  (`TRIAGE_REASONS`), accuracy: the share of those captures on which the run and the reference
  agree about whether that reason applies. A capture the run did not store counts as kept.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from itertools import combinations

from pydantic import BaseModel, computed_field

TRIAGE_REASONS: tuple[str, ...] = ("blank", "duplicate", "blurry", "partial", "same_content")
"""The triage reasons scored, in report order (`sources.triage.TRIAGE_REASONS`)."""

# A reference unit is kept when one generated unit holds this share of its content words.
COVERED_SHARE = 0.5
# A generated unit is supported when this share of its content words is in the sources.
SUPPORTED_SHARE = 0.8
# Units with fewer content words than this say too little to score (a lone "Ejemplo:").
MIN_UNIT_WORDS = 2

_UNREADABLE = re.compile(r"\[\[\?([^\]]*)\]\]")
_FOOTNOTE_REF = re.compile(r"\[\^[^\]\s]+\]")
_FOOTNOTE_DEF = re.compile(r"^\s*\[\^[^\]\s]+\]:")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_ANCHOR = re.compile(r"\{#[^}]*\}")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
_NOT_WORD = re.compile(r"[^\w\s]|_")
_WORD = re.compile(r"\w+")

_STOPWORDS_TEXT = """
ante bajo cabe con contra desde durante entre hacia hasta mediante para por segun sin
sobre tras los las del una unos unas uno que como cuando donde quien cual cuales cuyo pero
sino porque pues aunque mas muy tan tanto tambien tampoco solo ya aun asi este esta estos
estas ese esa esos esas aquel aquella esto eso ello ella ellos ellas nos vos usted ustedes
mis tus sus nuestro nuestra vuestro vuestra ser son era eran sera fue fueron sido estar
estan estaba hay haber has han habia tiene tienen tener hace hacen puede pueden todo toda
todos todas otro otra otros otras mismo misma algo nada cada vez veces bien mal aqui ahi
alli entonces luego
"""
STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


def normalize(text: str) -> str:
    """`text` as the rubric compares it: plain words, lower case, no accents, single spaces."""
    text = _UNREADABLE.sub(r"\1", text)
    text = _FOOTNOTE_REF.sub(" ", text)
    text = _ANCHOR.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(_NOT_WORD.sub(" ", folded.lower()).split())


def words(text: str) -> list[str]:
    """The normalized words of `text`, in order."""
    return _WORD.findall(normalize(text))


def content_words(text: str) -> set[str]:
    """The words of `text` that carry content (see the module docstring)."""
    return {w for w in words(text) if w.isdigit() or (len(w) >= 3 and w not in STOPWORDS)}


def levenshtein(a: list[str] | str, b: list[str] | str) -> int:
    """The edit distance between two sequences (characters or words)."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, item in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (item != other))
            )
        previous = current
    return previous[-1]


def _accuracy(distance: int, length: int) -> float:
    if length == 0:
        return 1.0 if distance == 0 else 0.0
    return max(0.0, 1.0 - distance / length)


class PageScore(BaseModel):
    """One captured page: its transcription against the reference text."""

    capture_id: str
    # False when the pipeline produced no transcription of the page (both accuracies are 0).
    transcribed: bool
    char_accuracy: float
    word_accuracy: float


def score_page(capture_id: str, reference: str, transcription: str | None) -> PageScore:
    """Score one page's transcription (`None`: never transcribed) against its reference."""
    if transcription is None:
        return PageScore(
            capture_id=capture_id, transcribed=False, char_accuracy=0.0, word_accuracy=0.0
        )
    ref, got = normalize(reference), normalize(transcription)
    ref_words, got_words = words(reference), words(transcription)
    return PageScore(
        capture_id=capture_id,
        transcribed=True,
        char_accuracy=round(_accuracy(levenshtein(ref, got), len(ref)), 4),
        word_accuracy=round(_accuracy(levenshtein(ref_words, got_words), len(ref_words)), 4),
    )


class SectionScore(BaseModel):
    """The observer's outline against the reference sections, over the reference's segments."""

    segments: int
    assigned: int
    coverage: float
    # `None` with fewer than two segments: there is no pair to agree on.
    pairwise_agreement: float | None
    reference_sections: int
    observer_sections: int


def score_sections(
    reference: Iterable[tuple[str, Iterable[str]]], assignments: Mapping[str, str]
) -> SectionScore:
    """Score `assignments` (segment id -> observer section id) against `reference`.

    `reference` is `(title, segment ids)` per section; only its segments are scored, so a
    segment the student left out of the reference (small talk) costs nothing.
    """
    truth: dict[str, str] = {}
    sections = 0
    for title, segments in reference:
        sections += 1
        for segment in segments:
            truth[segment] = f"{sections}:{title}"
    segments = list(truth)
    assigned = [s for s in segments if s in assignments]
    agree = pairs = 0
    for a, b in combinations(segments, 2):
        pairs += 1
        together = truth[a] == truth[b]
        observed = a in assignments and assignments.get(a) == assignments.get(b)
        agree += together == observed
    return SectionScore(
        segments=len(segments),
        assigned=len(assigned),
        coverage=round(len(assigned) / len(segments), 4) if segments else 1.0,
        pairwise_agreement=round(agree / pairs, 4) if pairs else None,
        reference_sections=sections,
        observer_sections=len({assignments[s] for s in assigned}),
    )


def units(markdown: str) -> list[str]:
    """The scoring units of notes: list items and sentences, no headings or footnote definitions.

    Units with fewer than `MIN_UNIT_WORDS` content words are left out.
    """
    found: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            found.extend(_SENTENCE_END.split(_FOOTNOTE_REF.sub("", " ".join(paragraph))))
            paragraph.clear()

    in_footnote = False
    for line in markdown.splitlines():
        if not line.strip():
            flush()
            in_footnote = False
            continue
        if _FOOTNOTE_DEF.match(line):
            flush()
            in_footnote = True
            continue
        if in_footnote and line.startswith((" ", "\t")):
            continue
        in_footnote = False
        if _HEADING.match(line):
            flush()
            continue
        if _LIST_MARK.match(line):
            flush()
            paragraph.append(_LIST_MARK.sub("", line))
            continue
        paragraph.append(line.strip())
    flush()
    return [u.strip() for u in found if len(content_words(u)) >= MIN_UNIT_WORDS]


class NotesFidelity(BaseModel):
    """The generated notes against the reference notes and the session's own material."""

    reference_units: int
    generated_units: int
    kept: float
    supported: float
    # The reference units no generated unit covers, and the generated units the sources do not
    # support, as written: what a person reads to see why a score dropped.
    dropped: list[str]
    unsupported: list[str]


def score_notes(reference: str, generated: str, sources: Iterable[str]) -> NotesFidelity:
    """Score `generated` notes against `reference` notes; `sources` is the session's material
    (transcript segments, page transcriptions) the notes may draw from."""
    reference_units = units(reference)
    generated_units = units(generated)
    generated_words = [content_words(u) for u in generated_units]
    dropped: list[str] = []
    for unit in reference_units:
        wanted = content_words(unit)
        best = max((len(wanted & got) / len(wanted) for got in generated_words), default=0.0)
        if best < COVERED_SHARE:
            dropped.append(unit)
    vocabulary = content_words(reference)
    for source in sources:
        vocabulary |= content_words(source)
    unsupported = [
        unit
        for unit, got in zip(generated_units, generated_words, strict=True)
        if len(got & vocabulary) / len(got) < SUPPORTED_SHARE
    ]
    return NotesFidelity(
        reference_units=len(reference_units),
        generated_units=len(generated_units),
        kept=_share(len(reference_units) - len(dropped), len(reference_units)),
        supported=_share(len(generated_units) - len(unsupported), len(generated_units)),
        dropped=dropped,
        unsupported=unsupported,
    )


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 1.0


class RequestItem(BaseModel):
    """One request to the assistant, detected or in the reference, as the report lists it."""

    kind: str
    segments: list[str]
    # Detected: the detector's summary; reference: the student's note (or the kind).
    summary: str
    # The finals' text the request spans.
    text: str


class KindScore(BaseModel):
    """Request detection of one kind."""

    kind: str
    reference: int
    detected: int
    matched: int
    precision: float
    recall: float
    f1: float


class RequestScore(BaseModel):
    """The detected requests against the reference requests of one case."""

    reference: int
    detected: int
    matched: int
    precision: float
    recall: float
    f1: float
    per_kind: list[KindScore]
    # Reference requests no detected one matches, and detected ones matching none.
    missed: list[RequestItem]
    spurious: list[RequestItem]


def _f1(precision: float, recall: float) -> float:
    total = precision + recall
    return round(2 * precision * recall / total, 4) if total else 0.0


def score_requests(
    reference: Iterable[RequestItem], detected: Iterable[RequestItem]
) -> RequestScore:
    """Match `detected` requests to `reference` ones (same kind, one shared segment at least).

    Each request matches at most once: every reference request, in order, takes the first
    detected one still free that matches it. With nothing detected precision is 1; with nothing
    to detect recall is 1.
    """
    wanted, found = list(reference), list(detected)
    taken: set[int] = set()
    missed: list[RequestItem] = []
    matched_kinds: list[str] = []
    for request in wanted:
        segments = set(request.segments)
        index = next(
            (
                i
                for i, other in enumerate(found)
                if i not in taken and other.kind == request.kind and segments & set(other.segments)
            ),
            None,
        )
        if index is None:
            missed.append(request)
        else:
            taken.add(index)
            matched_kinds.append(request.kind)
    spurious = [request for i, request in enumerate(found) if i not in taken]
    per_kind: list[KindScore] = []
    kinds = dict.fromkeys([r.kind for r in wanted] + [r.kind for r in found])
    for kind in kinds:
        ref = sum(r.kind == kind for r in wanted)
        det = sum(r.kind == kind for r in found)
        hit = matched_kinds.count(kind)
        precision, recall = _share(hit, det), _share(hit, ref)
        per_kind.append(
            KindScore(
                kind=kind,
                reference=ref,
                detected=det,
                matched=hit,
                precision=precision,
                recall=recall,
                f1=_f1(precision, recall),
            )
        )
    precision = _share(len(matched_kinds), len(found))
    recall = _share(len(matched_kinds), len(wanted))
    return RequestScore(
        reference=len(wanted),
        detected=len(found),
        matched=len(matched_kinds),
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        per_kind=per_kind,
        missed=missed,
        spurious=spurious,
    )


class TriageItem(BaseModel):
    """One capture's triage, in the reference or as the run left it."""

    capture_id: str
    status: str
    reasons: list[str] = []


class ReasonScore(BaseModel):
    """How well one triage reason was given, over the reference's captures."""

    reason: str
    reference: int
    detected: int
    matched: int
    accuracy: float


class TriageScore(BaseModel):
    """The run's triage of a case's captures against the reference triage."""

    captures: int
    reference_set_aside: int
    set_aside: int
    matched: int
    precision: float
    recall: float
    f1: float
    per_reason: list[ReasonScore]
    # Reference captures whose status or reasons the run got differently, and those it never
    # stored (as the run left them: `status` "missing").
    mismatches: list[tuple[TriageItem, TriageItem]]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reason_accuracy(self) -> float | None:
        """The mean accuracy over the reasons, `None` for a case with no capture."""
        if not self.captures:
            return None
        return round(sum(r.accuracy for r in self.per_reason) / len(self.per_reason), 4)


MISSING_STATUS = "missing"


def score_triage(
    reference: Iterable[TriageItem], observed: Mapping[str, TriageItem]
) -> TriageScore:
    """Score `observed` (capture id -> the run's triage) against every `reference` capture.

    With nothing set aside by the run precision is 1; with nothing to set aside recall is 1.
    """
    wanted = list(reference)
    got = {
        item.capture_id: observed.get(item.capture_id)
        or TriageItem(capture_id=item.capture_id, status=MISSING_STATUS)
        for item in wanted
    }
    ref_aside = {i.capture_id for i in wanted if i.status == "set_aside"}
    run_aside = {c for c, i in got.items() if i.status == "set_aside"}
    hits = len(ref_aside & run_aside)
    precision, recall = _share(hits, len(run_aside)), _share(hits, len(ref_aside))
    per_reason: list[ReasonScore] = []
    for reason in TRIAGE_REASONS:
        expected = {i.capture_id for i in wanted if reason in i.reasons}
        given = {c for c, i in got.items() if reason in i.reasons}
        agree = sum((c in expected) == (c in given) for c in got)
        per_reason.append(
            ReasonScore(
                reason=reason,
                reference=len(expected),
                detected=len(given),
                matched=len(expected & given),
                accuracy=_share(agree, len(wanted)),
            )
        )
    mismatches = [
        (item, got[item.capture_id])
        for item in wanted
        if (item.status, sorted(item.reasons))
        != (got[item.capture_id].status, sorted(got[item.capture_id].reasons))
    ]
    return TriageScore(
        captures=len(wanted),
        reference_set_aside=len(ref_aside),
        set_aside=len(run_aside),
        matched=hits,
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        per_reason=per_reason,
        mismatches=mismatches,
    )
