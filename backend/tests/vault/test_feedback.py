"""The feedback inbox (`vault.feedback`, #472): append, ids, fold-on-read of status changes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from secret_samples import ANTHROPIC_KEY

from studentassistant.vault import (
    FeedbackContext,
    FeedbackNotFoundError,
    Vault,
    add_feedback,
    feedback_path,
    get_feedback,
    list_feedback,
    set_feedback_status,
)
from studentassistant.vault.secrets import SecretRefused

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def _clock(minutes: int = 0):  # noqa: ANN202
    return lambda: T0 + timedelta(minutes=minutes)


def _lines(vault: Vault) -> list[dict]:
    return [json.loads(line) for line in feedback_path(vault).read_text("utf-8").splitlines()]


def test_an_empty_vault_has_no_inbox(tmp_vault: Vault) -> None:
    assert list_feedback(tmp_vault) == []
    assert not feedback_path(tmp_vault).exists()
    with pytest.raises(FeedbackNotFoundError):
        get_feedback(tmp_vault, "fb-1")


def test_items_are_appended_with_consecutive_ids(tmp_vault: Vault) -> None:
    context = FeedbackContext(
        subject="calculo",
        topic="derivadas",
        route="workspace",
        session_id="s-1",
        mode="construir",
        excerpt="Estudiante: apunta una mejora",
    )
    first = add_feedback(
        tmp_vault, "mejora", "  Poder  exportar\n a PDF ", " «quiero PDF» ", context, clock=_clock()
    )
    second = add_feedback(
        tmp_vault, "bug", "La foto no se guarda", "«no se guarda»", clock=_clock(1)
    )

    assert (first.id, first.status, first.issue) == ("fb-1", "nuevo", None)
    assert first.title == "Poder exportar a PDF" and first.body == "«quiero PDF»"
    assert first.created_at == T0 and first.context == context
    assert second.id == "fb-2" and second.context == FeedbackContext()
    assert feedback_path(tmp_vault).relative_to(tmp_vault.path).as_posix() == "feedback/inbox.jsonl"
    lines = _lines(tmp_vault)
    assert [(line["record"], line["id"]) for line in lines] == [("item", "fb-1"), ("item", "fb-2")]
    assert lines[0]["context"]["mode"] == "construir"
    assert [item.id for item in list_feedback(tmp_vault)] == ["fb-1", "fb-2"]


def test_status_changes_are_appended_and_folded_on_read(tmp_vault: Vault) -> None:
    add_feedback(tmp_vault, "mejora", "Uno", "uno", clock=_clock())
    add_feedback(tmp_vault, "bug", "Dos", "dos", clock=_clock())
    before = feedback_path(tmp_vault).read_bytes()

    triaged = set_feedback_status(tmp_vault, "fb-1", "triado", issue=480, clock=_clock(5))
    assert (triaged.status, triaged.issue, triaged.updated_at) == ("triado", 480, _clock(5)())
    # Append-only: the earlier lines are untouched.
    assert feedback_path(tmp_vault).read_bytes().startswith(before)

    # `issue=None` keeps the reference the item already has.
    back = set_feedback_status(tmp_vault, "fb-1", "nuevo", clock=_clock(6))
    assert (back.status, back.issue) == ("nuevo", 480)
    set_feedback_status(tmp_vault, "fb-2", "descartado", clock=_clock(7))

    assert [line["record"] for line in _lines(tmp_vault)] == [
        "item",
        "item",
        "status",
        "status",
        "status",
    ]
    item = get_feedback(tmp_vault, "fb-1")
    assert (item.status, item.issue, item.updated_at) == ("nuevo", 480, _clock(6)())
    assert [i.id for i in list_feedback(tmp_vault, "nuevo")] == ["fb-1"]
    assert [i.id for i in list_feedback(tmp_vault, "descartado")] == ["fb-2"]
    assert list_feedback(tmp_vault, "triado") == []


def test_a_merged_inbox_folds_in_file_order(tmp_vault: Vault) -> None:
    """Lines of another PC merged with `union`: a repeated id keeps its first item."""
    add_feedback(tmp_vault, "mejora", "Primero", "uno", clock=_clock())
    path = feedback_path(tmp_vault)
    with path.open("a", encoding="utf-8") as inbox:
        other = {
            "record": "item",
            "id": "fb-1",
            "created_at": T0.isoformat(),
            "kind": "bug",
            "title": "Del otro PC",
            "body": "otro",
            "context": {},
        }
        change = {"record": "status", "id": "fb-9", "time": T0.isoformat(), "status": "triado"}
        inbox.write(json.dumps(other) + "\n" + json.dumps(change) + "\n")

    [item] = list_feedback(tmp_vault)
    assert (item.title, item.status) == ("Primero", "nuevo")
    assert add_feedback(tmp_vault, "bug", "Tres", "tres").id == "fb-2"


def test_bad_writes_leave_the_inbox_as_it_was(tmp_vault: Vault) -> None:
    with pytest.raises(FeedbackNotFoundError):
        set_feedback_status(tmp_vault, "fb-1", "triado")
    with pytest.raises(FeedbackNotFoundError):
        set_feedback_status(tmp_vault, "issue-1", "triado")
    with pytest.raises(ValidationError):
        add_feedback(tmp_vault, "idea", "Título", "cuerpo")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        add_feedback(tmp_vault, "bug", "   ", "cuerpo")
    with pytest.raises(SecretRefused):
        add_feedback(tmp_vault, "bug", "Clave", ANTHROPIC_KEY)
    assert list_feedback(tmp_vault) == []
