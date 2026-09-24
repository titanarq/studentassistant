"""JSONL logs: one object per line, a torn last line tolerated, a secret never appended."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from secret_samples import ANTHROPIC_KEY

from studentassistant.vault import jsonl
from studentassistant.vault.jsonl import JsonlError, append_jsonl, last_seq, read_jsonl
from studentassistant.vault.secrets import SecretRefused
from studentassistant.vault.session_models import Event

N = 5


def event(seq: int) -> Event:
    return Event(seq=seq, t=seq * 100, origin="stt", kind="segment_final", payload={"n": seq})


def write_n(path: Path, n: int = N) -> list[Event]:
    events = [event(seq) for seq in range(1, n + 1)]
    for item in events:
        append_jsonl(path, item)
    return events


def test_n_objects_written_are_n_compact_lines_read_back_in_order(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = write_n(path)

    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    lines = raw.splitlines()
    assert len(lines) == N
    assert all(": " not in line and ", " not in line for line in lines)
    assert json.loads(lines[0])["seq"] == 1
    assert list(read_jsonl(path, Event)) == events


def test_a_plain_mapping_is_appended_as_is_with_unicode_kept(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    append_jsonl(path, {"seq": 1, "texto": "límite"})

    assert path.read_text(encoding="utf-8") == '{"seq":1,"texto":"límite"}\n'


def test_a_line_torn_mid_way_is_left_out_without_an_exception(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = write_n(path)
    content = path.read_bytes()
    path.write_bytes(content[: len(content) - 7])

    assert list(read_jsonl(path, Event)) == events[:-1]
    assert last_seq(path) == N - 1


def test_the_next_append_cuts_a_torn_tail_off_instead_of_joining_it(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = write_n(path)
    content = path.read_bytes()
    path.write_bytes(content[: len(content) - 7])

    append_jsonl(path, event(N))

    assert list(read_jsonl(path, Event)) == events


def test_a_torn_tail_longer_than_the_scan_chunk_is_cut_off_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jsonl, "_TAIL_CHUNK", 8)
    path = tmp_path / "events.jsonl"
    write_n(path, 2)
    with path.open("ab") as log:
        log.write(b'{"seq":3,"t":300,"origin":"stt","kind":"k","pay')

    append_jsonl(path, event(3))

    assert [item.seq for item in read_jsonl(path, Event)] == [1, 2, 3]


def test_a_file_holding_only_a_torn_line_reads_as_empty_and_is_repaired(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"seq":1,"t":')

    assert list(read_jsonl(path, Event)) == []
    assert last_seq(path) == 0
    append_jsonl(path, event(1))
    assert list(read_jsonl(path, Event)) == [event(1)]


def test_last_seq_of_an_empty_and_of_a_missing_file_is_zero(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")

    assert last_seq(empty) == 0
    assert last_seq(tmp_path / "missing.jsonl") == 0


def test_last_seq_is_the_highest_seq_even_after_a_union_merge_reordered_lines(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    for seq in (1, 4, 2, 3):
        append_jsonl(path, event(seq))

    assert last_seq(path) == 4


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    append_jsonl(path, event(1))
    with path.open("ab") as log:
        log.write(b"\n")
    append_jsonl(path, event(2))

    assert [item.seq for item in read_jsonl(path, Event)] == [1, 2]


def test_a_complete_line_that_is_not_json_is_refused_with_its_line_number(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    append_jsonl(path, event(1))
    with path.open("ab") as log:
        log.write(b"not json\n")

    with pytest.raises(JsonlError, match=r"events\.jsonl:2"):
        list(read_jsonl(path, Event))


def test_a_line_carrying_a_secret_is_refused_and_not_appended(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    write_n(path, 2)
    before = path.read_bytes()

    with pytest.raises(SecretRefused):
        append_jsonl(path, {"seq": 3, "text": f"mi clave es {ANTHROPIC_KEY}"})

    assert path.read_bytes() == before


def test_a_refused_first_line_does_not_even_create_the_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    with pytest.raises(SecretRefused):
        append_jsonl(path, {"seq": 1, "text": ANTHROPIC_KEY})

    assert not path.exists()


def test_each_append_is_fsynced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    real_fsync = os.fsync

    def record(descriptor: int) -> None:
        synced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(jsonl.os, "fsync", record)
    write_n(tmp_path / "events.jsonl", 3)

    assert len(synced) == 3
