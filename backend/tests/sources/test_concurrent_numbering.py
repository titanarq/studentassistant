"""Concurrent writers of one topic's sources get distinct numbers and never overwrite each other."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pdf_samples import make_pdf

import studentassistant.vault.sources as vault_sources
from studentassistant.sources import import_pdf
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    list_sources,
    put_source,
    sources_directory,
)

WRITERS_PER_KIND = 4
TIMEOUT_SECONDS = 30


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Historia").slug
    return subject, create_topic(tmp_vault, subject, "La Revolución Industrial").slug


@pytest.fixture
def slow_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the gap between scanning for the next number and writing under it.

    Without the per-directory lock every writer released by the barrier below would see the same
    highest number here and store under it.
    """
    scan = vault_sources._next_number

    def slow(directory: Path, pattern: object) -> int:
        number = scan(directory, pattern)  # type: ignore[arg-type]
        time.sleep(0.02)
        return number

    monkeypatch.setattr(vault_sources, "_next_number", slow)


@pytest.mark.usefixtures("slow_scan")
def test_concurrent_pdf_imports_and_put_source_get_distinct_numbers(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    writers = 3 * WRITERS_PER_KIND
    barrier = threading.Barrier(writers, timeout=TIMEOUT_SECONDS)

    def pdf_import(index: int) -> tuple[Path, bytes | None]:
        content = make_pdf(index + 1)
        barrier.wait()
        return import_pdf(tmp_vault, *topic, f"libro-{index}.pdf", content).path, None

    def pdf_put(index: int) -> tuple[Path, bytes | None]:
        content = b"%PDF-1.7\n" + bytes([index]) * 64
        barrier.wait()
        return put_source(
            tmp_vault, *topic, "pdf", "otro.pdf", content, {"writer": f"put-{index}"}
        ), content

    def notes_put(index: int) -> tuple[Path, bytes | None]:
        content = b"\xff\xd8\xff" + bytes([index]) * 64
        barrier.wait()
        return put_source(
            tmp_vault, *topic, "notes", "foto.jpg", content, {"capture_id": f"c{index}"}
        ), content

    with ThreadPoolExecutor(max_workers=writers) as pool:
        futures = [
            pool.submit(writer, index)
            for writer in (pdf_import, pdf_put, notes_put)
            for index in range(WRITERS_PER_KIND)
        ]
        results = [future.result(timeout=TIMEOUT_SECONDS) for future in futures]

    paths = [path for path, _ in results]
    assert len(set(paths)) == writers
    for path, content in results:
        if content is not None:
            assert path.read_bytes() == content
    pdf_names = sorted(p.name for p in paths if p.parent.name == "pdf")
    assert pdf_names == [f"page-{n:03d}.pdf" for n in range(1, 2 * WRITERS_PER_KIND + 1)]
    notes = sources_directory(tmp_vault, *topic, "notes")
    assert sorted(p.name for p in paths if p.parent == notes) == [
        f"page-{n:03d}.jpg" for n in range(1, WRITERS_PER_KIND + 1)
    ]
    listed = list_sources(tmp_vault, *topic)
    assert len(listed) == writers
    metas = [source.meta or {} for source in listed]
    assert sorted(m["original_name"] for m in metas if "original_name" in m) == [
        f"libro-{index}.pdf" for index in range(WRITERS_PER_KIND)
    ]
    assert sorted(m["writer"] for m in metas if "writer" in m) == [
        f"put-{index}" for index in range(WRITERS_PER_KIND)
    ]
    assert sorted(m["capture_id"] for m in metas if "capture_id" in m) == [
        f"c{index}" for index in range(WRITERS_PER_KIND)
    ]
