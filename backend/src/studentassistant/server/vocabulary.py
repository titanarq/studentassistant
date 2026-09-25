"""The terms a session's vocabulary hints are built from (#54): vault metadata + observer state.

`load_topic_terms` reads, through the vault's and the observer's public functions, the subject's
name, the topic's title and the concepts the observer has extracted for the topic so far (with the
open pending count, which a hints-carrying `notice` has to repeat). `SessionVocabulary` keeps
those for one socket and turns them into hints (`stt.vocabulary_hints_from_settings`) as the
observer's `add_concept` ops arrive on the bus. Hint assembly itself is `stt`'s; this module only
gathers the inputs, so `stt` never imports the observer or the vault.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from studentassistant.config import SttSettings
from studentassistant.observer import ObserverStateError, load_observer_snapshot
from studentassistant.stt import vocabulary_hints_from_settings
from studentassistant.vault import Vault, VaultError, get_subject, get_topic

logger = logging.getLogger(__name__)

ADD_CONCEPT_OP = "add_concept"


@dataclass
class TopicTerms:
    """What a topic's hints come from; each part is empty (or `None`) when it cannot be read."""

    subject: str | None = None
    topic: str | None = None
    # Concept id -> name, in the order the observer added them.
    concepts: dict[str, str] = field(default_factory=dict)
    # Open pending-review items, `None` when the observer state cannot be read.
    pending_count: int | None = None


def load_topic_terms(vault: Vault, subject_id: str, topic_id: str) -> TopicTerms:
    """The topic's names and observer concepts (blocking: run it in a worker thread).

    Never raises for unreadable data: a part that cannot be read is logged and left empty, so a
    damaged file costs the hints, never the session.
    """
    terms = TopicTerms()
    try:
        terms.subject = get_subject(vault, subject_id).subject.name
    except VaultError as error:
        logger.warning("subject %s: name unreadable for hints: %s", subject_id, error)
    try:
        terms.topic = get_topic(vault, subject_id, topic_id).topic.title
    except VaultError as error:
        logger.warning("topic %s/%s: title unreadable for hints: %s", subject_id, topic_id, error)
    try:
        snapshot = load_observer_snapshot(vault, subject_id, topic_id, write_back=False)
    except (VaultError, ObserverStateError) as error:
        logger.warning("topic %s/%s: observer state unreadable: %s", subject_id, topic_id, error)
    else:
        state = snapshot.state
        terms.concepts = {concept.id: concept.name for concept in state.concepts.values()}
        terms.pending_count = len(state.open_pending())
    return terms


class SessionVocabulary:
    """One socket's view of the session's hints, updated from the observer's ops."""

    def __init__(self, settings: SttSettings, terms: TopicTerms) -> None:
        self.settings = settings
        self.terms = terms
        self.hints = self._build()

    def _build(self) -> list[str]:
        return vocabulary_hints_from_settings(
            self.settings,
            subject=self.terms.subject,
            topic=self.terms.topic,
            concepts=self.terms.concepts.values(),
        )

    def apply_state_op(self, payload: Mapping[str, Any]) -> bool:
        """Take an `observer.state_op` payload; True when it changed the hints.

        Only `add_concept` matters (a concept id already known keeps its first name, as the fold
        refuses a duplicate id); any other op, or a payload that is not a usable one, is ignored.
        """
        if payload.get("op") != ADD_CONCEPT_OP:
            return False
        concept_id, name = payload.get("concept_id"), payload.get("name")
        if not isinstance(concept_id, str) or not isinstance(name, str) or not name.strip():
            return False
        if concept_id in self.terms.concepts:
            return False
        self.terms.concepts[concept_id] = name
        hints = self._build()
        if hints == self.hints:
            return False
        self.hints = hints
        return True


__all__ = ["ADD_CONCEPT_OP", "SessionVocabulary", "TopicTerms", "load_topic_terms"]
