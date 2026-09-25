"""`docs/ADOPTION.md` agrees with the code it tells a second host to satisfy (agent-os#7).

The checklist is prose a host follows verbatim, so a name it leaves out is a first-run failure
nobody sees until `agent-os-doctor` reports it. The expected values come from the code itself --
the doctor's own verdict -- never from a list copied into this file.

Pure filesystem plus mocked subprocess. This file must not request the `engine` or `db_sandbox`
fixture, and must never let a real `gh` run.
"""

from __future__ import annotations

import json
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from agent_os import doctor
from agent_os.cli import AGENT_OS_DIR
from agent_os.lib import LabelVocabulary

ADOPTION = AGENT_OS_DIR / "docs" / "ADOPTION.md"


def _adoption_step(number: int) -> str:
    """The text of one numbered step, up to the next numbered step or heading. Fails loudly when
    the step is not there, instead of checking an empty string."""
    text = ADOPTION.read_text()
    match = re.search(rf"^{number}\. (.*?)(?=^\d+\. |^#)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, f"{ADOPTION} has no step {number}"
    return match.group(1)


def _labels_the_doctor_requires(vocabulary: LabelVocabulary) -> list[str]:
    """Every vocabulary label whose absence alone fails `doctor.check_labels`: asked of the check
    one label at a time, so the answer is the doctor's behaviour, not a reading of its source."""
    project = SimpleNamespace(labels=vocabulary)
    every_label = sorted(
        {value for value in vocabulary.model_dump().values() if isinstance(value, str)}
    )

    def passes_with(present: list[str]) -> bool:
        listing = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=json.dumps([{"name": n} for n in present]), stderr=""
        )
        with patch("agent_os.issues.subprocess.run", return_value=listing):
            return doctor.check_labels(project, "owner/name").ok

    # If the whole vocabulary does not satisfy the check, it requires a label this walk cannot
    # see, and the per-label answer below would be meaningless.
    assert passes_with(every_label), "check_labels requires a label outside LabelVocabulary"
    return [label for label in every_label if not passes_with(sorted(set(every_label) - {label}))]


def test_the_doctor_requires_no_state_label():
    # Every state label is created by `issues.py move` on first use; a doctor that required one
    # would fail a healthy host that simply has not reached that state yet (#54).
    vocabulary = LabelVocabulary()
    autocreated = set(_labels_the_doctor_requires(vocabulary)) & set(vocabulary.state_labels)
    assert not autocreated, f"agent-os-doctor requires state label(s) {sorted(autocreated)}"


def test_step_12_names_every_label_the_doctor_requires():
    required = _labels_the_doctor_requires(LabelVocabulary())
    assert required, "check_labels requires no label at all -- this test checks nothing"
    step = _adoption_step(12)
    missing = [label for label in required if f"`{label}`" not in step]
    assert not missing, f"ADOPTION.md step 12 never names {missing}, which agent-os-doctor requires"
