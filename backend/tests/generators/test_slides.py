"""The slides generator: the Marp deck, its credited figures and the PDF/PPTX export."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import GeneratorsSettings, Settings
from studentassistant.generators import (
    GenerateResult,
    GeneratorRegistry,
    default_registry,
    read_artifact_meta,
    run_generator,
)
from studentassistant.generators.slides import (
    KIND,
    MARKDOWN_NAME,
    PDF_NAME,
    PPTX_NAME,
    TOOL_NAME,
    DraftDeck,
    DraftSlide,
    Exported,
    MarpExporter,
    PlacedFigure,
    SlidesGenerator,
    render_markdown,
)
from studentassistant.llm import FakeClaude, LedgerBinding
from studentassistant.vault import GitSync, Vault, generated_directory

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)

DECK: dict[str, Any] = {
    "title": "Derivadas",
    "subtitle": "Definición y notación",
    "slides": [
        {
            "title": "Qué es la derivada",
            "bullets": ["**Derivada**: el límite del cociente incremental.", "Se escribe $f'(x)$."],
            "notes": "Empezamos por la definición.",
            "anchors": ["definicion"],
            "image": "f1",
        },
        {
            "title": "Próximo día",
            "bullets": ["La regla de la cadena."],
            "anchors": ["#proximo-dia"],
        },
    ],
}


class FakeExporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, bytes]]] = []

    async def export(self, markdown: str, assets: dict[str, bytes]) -> Exported:
        self.calls.append((markdown, assets))
        return Exported(files={PDF_NAME: b"%PDF-1.7 fake", PPTX_NAME: b"PK fake pptx"})


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> FakeExporter:
    fake_exporter = FakeExporter()
    monkeypatch.setattr(SlidesGenerator, "exporter", fake_exporter)
    return fake_exporter


def _registry() -> GeneratorRegistry:
    registry = GeneratorRegistry()
    registry.register(SlidesGenerator)
    return registry


def _generate(
    topic: ReviseTopic, fake: FakeClaude, deck: dict[str, Any], **options: object
) -> GenerateResult:
    fake.reply_tool(TOOL_NAME, deck)
    client = fake.client("generator", ledger=LedgerBinding(topic.vault, topic.subject, topic.topic))
    return _run(
        run_generator(
            topic.vault,
            topic.subject,
            topic.topic,
            KIND,
            client=client,
            sync=GitSync(topic.vault),
            registry=_registry(),
            options=options,
            clock=lambda: NOW,
        )
    )


def _file(topic: ReviseTopic, name: str) -> bytes:
    return (generated_directory(topic.vault, topic.subject, topic.topic) / name).read_bytes()


def test_it_is_a_registered_generator() -> None:
    assert KIND in default_registry
    assert default_registry.lookup(KIND) is SlidesGenerator


def test_writes_the_marp_deck_its_figure_and_the_exports(
    topic: ReviseTopic, fake: FakeClaude, exporter: FakeExporter
) -> None:
    result = _generate(topic, fake, DECK)

    assert result.items == 2 and not result.unresolved and not result.warnings
    names = sorted(path.split("/generated/", 1)[1] for path in result.files)
    assert names == sorted(
        [MARKDOWN_NAME, PDF_NAME, PPTX_NAME, "diapositivas/figura-01.jpg", f"{KIND}.meta.yaml"]
    )
    markdown = _file(topic, MARKDOWN_NAME).decode("utf-8")
    assert markdown.startswith("---\nmarp: true\n")
    assert "# Derivadas\n\nDefinición y notación\n\nMatemáticas" in markdown
    assert "## Qué es la derivada\n\n- **Derivada**: el límite" in markdown
    assert "![bg right:40% contain](diapositivas/figura-01.jpg)" in markdown
    assert '<!-- _footer: "Imagen: Apuntes, página 1" -->' in markdown
    assert "<!--\nEmpezamos por la definición.\n-->" in markdown
    assert markdown.count("\n---\n") == 3  # front matter end + two slide breaks
    # The cropped page is what the slide shows.
    assert _file(topic, "diapositivas/figura-01.jpg") == b"\xff\xd8\xff\xe0 page one cropped"
    assert _file(topic, PDF_NAME) == b"%PDF-1.7 fake"

    shown = str(fake.requests[-1].messages)
    assert "`f1`: Apuntes, página 1 (citada en #definicion)" in shown
    assert "`f2`: Apuntes, página 2 (citada en #proximo-dia)" in shown
    assert "como mucho 12 diapositivas" in shown
    [(exported_markdown, assets)] = exporter.calls
    assert exported_markdown == markdown and list(assets) == ["diapositivas/figura-01.jpg"]

    meta = read_artifact_meta(topic.vault, topic.subject, topic.topic, KIND)
    assert meta is not None
    assert [(item.item, item.anchors) for item in meta.items] == [
        ("d01", ["definicion"]),
        ("d02", ["proximo-dia"]),
    ]


def test_export_can_be_skipped(
    topic: ReviseTopic, fake: FakeClaude, exporter: FakeExporter
) -> None:
    result = _generate(topic, fake, DECK, export=False)
    assert not exporter.calls
    assert not any(path.endswith((".pdf", ".pptx")) for path in result.files)


def test_a_failed_export_keeps_the_markdown_and_drops_old_exports(
    topic: ReviseTopic, fake: FakeClaude, exporter: FakeExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _generate(topic, fake, DECK)

    async def failing(markdown: str, assets: dict[str, bytes]) -> Exported:
        return Exported(warnings=["No se ha podido exportar las diapositivas a PDF: roto"])

    monkeypatch.setattr(exporter, "export", failing)
    result = _generate(topic, fake, {**DECK, "slides": DECK["slides"][1:]})
    assert result.warnings == ["No se ha podido exportar las diapositivas a PDF: roto"]
    assert any(path.endswith(MARKDOWN_NAME) for path in result.files)
    removed = sorted(path.split("/generated/", 1)[1] for path in result.removed)
    assert removed == sorted(["diapositivas/figura-01.jpg", PDF_NAME, PPTX_NAME])


def test_the_size_option_and_unknown_figures(
    topic: ReviseTopic, fake: FakeClaude, exporter: FakeExporter
) -> None:
    deck = {**DECK, "slides": [{**DECK["slides"][1], "image": "f9"}, DECK["slides"][0]]}
    result = _generate(topic, fake, deck, size=1)
    assert result.items == 1
    assert any("se guardan las 1 primeras" in warning for warning in result.warnings)
    assert any("(f9)" in warning for warning in result.warnings)
    assert "![bg" not in _file(topic, MARKDOWN_NAME).decode("utf-8")


def test_an_unknown_anchor_is_reported(
    topic: ReviseTopic, fake: FakeClaude, exporter: FakeExporter
) -> None:
    slide = {**DECK["slides"][1], "anchors": ["no-existe"]}
    result = _generate(topic, fake, {**DECK, "slides": [slide]})
    assert [entry.item for entry in result.unresolved] == ["d01"]


def test_render_markdown_keeps_comments_and_headings_safe() -> None:
    deck = DraftDeck(
        title="# Tema",
        slides=[
            DraftSlide(
                title="## Uno\ncon salto",
                bullets=["- ya con guion", "1. numerada", "  "],
                notes="cierra --> aquí",
                anchors=["a"],
                image="f1",
            )
        ],
    )
    figure = PlacedFigure(name="diapositivas/figura-01.png", credit='Libro, "página" 3 -- x')
    markdown = render_markdown(deck, {0: figure}, subject_name="Física")
    assert "# Tema\n\nFísica" in markdown
    assert "## Uno con salto\n\n- ya con guion\n- numerada\n" in markdown
    assert '<!-- _footer: "Imagen: Libro, \\"página\\" 3 – x" -->' in markdown
    assert "cierra → aquí" in markdown and "cierra -->" not in markdown


def test_the_configuration_builds_the_marp_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_GENERATORS__MARP_COMMAND", '["npx", "--yes", "@marp-team/marp-cli"]')
    monkeypatch.setenv("SA_GENERATORS__MARP_TIMEOUT_SECONDS", "30")
    exporter = MarpExporter.from_settings(Settings().generators)
    assert exporter.command == ["npx", "--yes", "@marp-team/marp-cli"]
    assert exporter.timeout_seconds == 30
    assert GeneratorsSettings().marp_command == ["marp"]


# --- MarpExporter against a stand-in `marp` script -----------------------------------------------

FAKE_MARP = """#!/bin/sh
# Records its arguments, checks the deck and its image are there, writes the output.
echo "$@" >> "{log}"
test -f diapositivas.md && test -f diapositivas/figura-01.jpg || exit 3
case "$*" in *--pptx*) {pptx} ;; esac
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output" ]; then printf 'exported %s' "$2" > "$2"; fi
  shift
done
"""


def _script(tmp_path: Path, pptx: str = ":") -> tuple[Path, Path]:
    log = tmp_path / "marp.log"
    script = tmp_path / "marp"
    script.write_text(FAKE_MARP.format(log=log, pptx=pptx), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


ASSETS = {"diapositivas/figura-01.jpg": b"img"}


def test_marp_exporter_runs_marp_for_pdf_and_pptx(tmp_path: Path) -> None:
    script, log = _script(tmp_path)
    exporter = MarpExporter(
        [str(script)], timeout_seconds=10, browser_path=Path("/usr/bin/chromium")
    )
    exported = _run(exporter.export("---\nmarp: true\n---\n", ASSETS))
    assert exported.warnings == []
    assert exported.files == {
        PDF_NAME: f"exported {PDF_NAME}".encode(),
        PPTX_NAME: f"exported {PPTX_NAME}".encode(),
    }
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        f"--allow-local-files {flag} --browser-path /usr/bin/chromium --output {name} "
        + MARKDOWN_NAME
        for flag, name in (("--pdf", PDF_NAME), ("--pptx", PPTX_NAME))
    ]


def test_marp_exporter_reports_a_failure_a_timeout_and_a_missing_command(tmp_path: Path) -> None:
    script, _ = _script(tmp_path, pptx="echo 'no chrome' >&2; exit 1")
    exported = _run(MarpExporter([str(script)], timeout_seconds=10).export("x", ASSETS))
    assert list(exported.files) == [PDF_NAME]
    assert exported.warnings == [
        "No se ha podido exportar las diapositivas a PPTX: Marp ha fallado (código 1): no chrome"
    ]

    slow, _ = _script(tmp_path, pptx="sleep 5")
    exported = _run(MarpExporter([str(slow)], timeout_seconds=0.5).export("x", ASSETS))
    assert list(exported.files) == [PDF_NAME]
    assert "no ha terminado en 0.5 segundos" in exported.warnings[0]

    missing = MarpExporter([str(tmp_path / "no-marp")], timeout_seconds=10)
    exported = _run(missing.export("x", ASSETS))
    assert exported.files == {}
    assert len(exported.warnings) == 1 and "no se encuentra Marp CLI" in exported.warnings[0]


@pytest.mark.integration
def test_real_marp_exports_pdf_and_pptx() -> None:
    command = os.environ.get("SA_TEST_MARP") or shutil.which("marp")
    if command is None:
        pytest.skip("Marp CLI not installed (set SA_TEST_MARP)")
    deck = DraftDeck(title="Derivadas", slides=[DraftSlide(title="Uno", bullets=["$f'(x)$"])])
    markdown = render_markdown(deck, {}, subject_name="Matemáticas")
    exported = _run(MarpExporter(command.split(), timeout_seconds=25).export(markdown, {}))
    assert exported.warnings == []
    assert exported.files[PDF_NAME].startswith(b"%PDF")
    assert exported.files[PPTX_NAME].startswith(b"PK")
