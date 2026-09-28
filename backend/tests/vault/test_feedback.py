"""The feedback inbox (`vault.feedback`, #472, #476): append, ids, fold-on-read of changes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from secret_samples import ANTHROPIC_KEY

from studentassistant.vault import (
    FeedbackAmbiguousError,
    FeedbackContext,
    FeedbackNotFoundError,
    Vault,
    add_feedback,
    feedback_path,
    get_feedback,
    list_feedback,
    set_feedback_status,
)
from studentassistant.vault.feedback import (
    FEEDBACK_ID_ALPHABET,
    FEEDBACK_ID_PATTERN,
    MAX_TITLE_CHARS,
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


def _item_line(feedback_id: str, title: str, minutes: int = 0) -> dict:
    return {
        "record": "item",
        "id": feedback_id,
        "created_at": (T0 + timedelta(minutes=minutes)).isoformat(),
        "kind": "bug",
        "title": title,
        "body": title.lower(),
        "context": {},
    }


def _status_line(feedback_id: str, status: str, minutes: int, issue: int | None = None) -> dict:
    time = (T0 + timedelta(minutes=minutes)).isoformat()
    return {"record": "status", "id": feedback_id, "time": time, "status": status, "issue": issue}


def _union(vault: Vault, *lines: dict) -> None:
    """Append lines as a `union` merge of another PC's inbox would leave them."""
    path = feedback_path(vault)
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as inbox:
        inbox.writelines(json.dumps(line) + "\n" for line in lines)


def test_items_are_appended_with_short_random_ids(tmp_vault: Vault) -> None:
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

    for item in (first, second):
        assert re.fullmatch(r"fb-[23456789abcdefghjkmnpqrstuvwxyz]{6}", item.id), item.id
    assert first.id != second.id
    assert (first.status, first.issue) == ("nuevo", None)
    assert first.title == "Poder exportar a PDF" and first.body == "«quiero PDF»"
    assert first.created_at == T0 and first.context == context
    assert second.context == FeedbackContext()
    assert feedback_path(tmp_vault).relative_to(tmp_vault.path).as_posix() == "feedback/inbox.jsonl"
    lines = _lines(tmp_vault)
    assert [(line["record"], line["id"]) for line in lines] == [
        ("item", first.id),
        ("item", second.id),
    ]
    assert lines[0]["context"]["mode"] == "construir"
    assert [item.id for item in list_feedback(tmp_vault)] == [first.id, second.id]


def test_the_id_alphabet_has_no_look_alikes() -> None:
    assert FEEDBACK_ID_ALPHABET.lower() == FEEDBACK_ID_ALPHABET
    assert not set("01ilo") & set(FEEDBACK_ID_ALPHABET)
    assert re.fullmatch(FEEDBACK_ID_PATTERN, "fb-12")  # a legacy id
    assert not re.fullmatch(FEEDBACK_ID_PATTERN, "fb-0")
    assert not re.fullmatch(FEEDBACK_ID_PATTERN, "fb-k7m2q")


def test_two_pcs_allocating_without_coordination_do_not_collide(
    tmp_vault: Vault, tmp_path: Path
) -> None:
    """Each PC adds to its own copy of the inbox; the union of both keeps every item apart."""
    other = Vault.init(tmp_path / "other-pc", student="Ana")
    for n in range(20):
        add_feedback(tmp_vault, "bug", f"Aquí {n}", "aquí", clock=_clock(n))
        add_feedback(other, "mejora", f"Allí {n}", "allí", clock=_clock(n))
    ours = [item.id for item in list_feedback(tmp_vault)]
    theirs = [item.id for item in list_feedback(other)]
    set_feedback_status(tmp_vault, ours[0], "triado", issue=900, clock=_clock(30))
    set_feedback_status(other, theirs[0], "descartado", clock=_clock(31))

    _union(tmp_vault, *_lines(other))

    items = list_feedback(tmp_vault)
    assert len(items) == len({item.id for item in items}) == 40
    assert get_feedback(tmp_vault, ours[0]).status == "triado"
    assert get_feedback(tmp_vault, ours[0]).title == "Aquí 0"
    assert get_feedback(tmp_vault, theirs[0]).status == "descartado"
    assert get_feedback(tmp_vault, theirs[0]).title == "Allí 0"
    assert [item.status for item in items].count("nuevo") == 38


def test_status_changes_are_appended_and_folded_on_read(tmp_vault: Vault) -> None:
    one = add_feedback(tmp_vault, "mejora", "Uno", "uno", clock=_clock()).id
    two = add_feedback(tmp_vault, "bug", "Dos", "dos", clock=_clock()).id
    before = feedback_path(tmp_vault).read_bytes()

    triaged = set_feedback_status(tmp_vault, one, "triado", issue=480, clock=_clock(5))
    assert (triaged.status, triaged.issue, triaged.updated_at) == ("triado", 480, _clock(5)())
    # Append-only: the earlier lines are untouched.
    assert feedback_path(tmp_vault).read_bytes().startswith(before)

    # `issue=None` keeps the reference the item already has.
    back = set_feedback_status(tmp_vault, one, "nuevo", clock=_clock(6))
    assert (back.status, back.issue) == ("nuevo", 480)
    set_feedback_status(tmp_vault, two, "descartado", clock=_clock(7))

    assert [line["record"] for line in _lines(tmp_vault)] == [
        "item",
        "item",
        "status",
        "status",
        "status",
    ]
    item = get_feedback(tmp_vault, one)
    assert (item.status, item.issue, item.updated_at) == ("nuevo", 480, _clock(6)())
    assert [i.id for i in list_feedback(tmp_vault, "nuevo")] == [one]
    assert [i.id for i in list_feedback(tmp_vault, "descartado")] == [two]
    assert list_feedback(tmp_vault, "triado") == []


def test_legacy_ids_are_still_read_folded_and_marked(tmp_vault: Vault) -> None:
    _union(
        tmp_vault,
        _item_line("fb-1", "Viejo uno"),
        _item_line("fb-2", "Viejo dos", minutes=1),
        _status_line("fb-1", "triado", 2, issue=470),
        _status_line("fb-9", "triado", 3),  # a change for no item is ignored
    )
    new = add_feedback(tmp_vault, "mejora", "Nuevo", "nuevo", clock=_clock(4))

    assert [item.id for item in list_feedback(tmp_vault)] == ["fb-1", "fb-2", new.id]
    assert (get_feedback(tmp_vault, "fb-1").status, get_feedback(tmp_vault, "fb-1").issue) == (
        "triado",
        470,
    )
    marked = set_feedback_status(tmp_vault, "fb-2", "descartado", clock=_clock(5))
    assert (marked.title, marked.status) == ("Viejo dos", "descartado")
    assert _lines(tmp_vault)[-1]["id"] == "fb-2"
    # The id is typed by hand: case and surrounding spaces do not matter.
    assert get_feedback(tmp_vault, f" {new.id.upper()} ").id == new.id
    assert set_feedback_status(tmp_vault, new.id.upper(), "triado").id == new.id


def test_a_legacy_id_two_pcs_both_allocated_keeps_both_items(tmp_vault: Vault) -> None:
    """Two PCs both wrote `fb-3` before syncing; the union merge holds both item lines."""
    _union(
        tmp_vault,
        _item_line("fb-3", "Del primer PC"),
        _status_line("fb-3", "triado", 2, issue=12),
        _item_line("fb-3", "Del segundo PC", minutes=1),
    )
    before = feedback_path(tmp_vault).read_bytes()

    items = list_feedback(tmp_vault)
    assert [(item.id, item.title) for item in items] == [
        ("fb-3", "Del primer PC"),
        ("fb-3", "Del segundo PC"),
    ]
    # The change came before the second item line in the file, so it folds only into the first.
    assert [item.status for item in items] == ["triado", "nuevo"]

    with pytest.raises(FeedbackAmbiguousError) as refused:
        set_feedback_status(tmp_vault, "fb-3", "descartado")
    assert refused.value.feedback_id == "fb-3"
    assert [item.title for item in refused.value.items] == ["Del primer PC", "Del segundo PC"]
    assert "Del primer PC" in str(refused.value) and "Del segundo PC" in str(refused.value)
    with pytest.raises(FeedbackAmbiguousError):
        get_feedback(tmp_vault, "fb-3")
    assert feedback_path(tmp_vault).read_bytes() == before

    # A change after both item lines (it cannot say which it meant) applies to both.
    _union(tmp_vault, _status_line("fb-3", "descartado", 5))
    assert [item.status for item in list_feedback(tmp_vault)] == ["descartado", "descartado"]


def test_the_title_limit_counts_the_collapsed_title(tmp_vault: Vault) -> None:
    exact = "x" * MAX_TITLE_CHARS
    assert add_feedback(tmp_vault, "bug", f"  {exact}\n", "cuerpo").title == exact
    # Longer than the limit as typed, within it once its runs of spaces are collapsed.
    spaced = "   ".join(["palabra"] * 17)
    assert len(spaced) > MAX_TITLE_CHARS
    assert add_feedback(tmp_vault, "bug", spaced, "cuerpo").title == " ".join(["palabra"] * 17)
    with pytest.raises(ValidationError):
        add_feedback(tmp_vault, "bug", exact + "y", "cuerpo")
    assert len(list_feedback(tmp_vault)) == 2


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
