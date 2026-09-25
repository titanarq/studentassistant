"""`studentassistant generate <kind> --topic <subject>/<topic>`: one material, committed."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

import studentassistant.cli as cli_module
from material_generators import DEFAULT_POINTS, KIND, PointsGenerator, reply_points
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.cli import cli
from studentassistant.generators import default_registry, read_artifact_meta
from studentassistant.llm import FakeClaude
from studentassistant.vault import Vault, create_topic, generated_directory


@pytest.fixture
def topic(tmp_vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReviseTopic:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    return make_revise_topic(tmp_vault)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeClaude]:
    fake = FakeClaude()
    monkeypatch.setattr(cli_module, "_generator_transport", lambda: fake)
    default_registry.register(PointsGenerator)
    try:
        yield fake
    finally:
        default_registry.unregister(KIND)


def _invoke(*args: str):
    return CliRunner().invoke(cli, ["generate", *args])


def test_generates_and_prints_the_files(topic: ReviseTopic, fake: FakeClaude) -> None:
    reply_points(fake, *DEFAULT_POINTS, ("Inventado", ["no-existe"]))

    result = _invoke(KIND, "--topic", f"{topic.subject}/{topic.topic}", "-o", "size=3")

    assert result.exit_code == 0, result.output
    assert f"Generado «{KIND}»" in result.output and "apuntes sin versión" in result.output
    assert f"generated/{KIND}.md" in result.output
    assert "Aviso: 1 elemento no cita" in result.output
    assert (generated_directory(topic.vault, topic.subject, topic.topic) / "prueba.md").is_file()
    meta = read_artifact_meta(topic.vault, topic.subject, topic.topic, KIND)
    assert meta is not None and meta.options["size"] == 3
    assert fake.requests[0].role == "generator"


def test_refusals(topic: ReviseTopic, fake: FakeClaude) -> None:
    where = f"{topic.subject}/{topic.topic}"
    unknown = _invoke("otro", "--topic", where)
    assert unknown.exit_code == 2 and "No hay ningún generador «otro»" in unknown.output
    bad_topic = _invoke(KIND, "--topic", "solo-uno")
    assert bad_topic.exit_code == 2
    missing = _invoke(KIND, "--topic", f"{topic.subject}/no-existe")
    assert missing.exit_code == 1 and "No existe el tema" in missing.output
    bad_option = _invoke(KIND, "--topic", where, "-o", "size")
    assert bad_option.exit_code == 2 and "clave=valor" in bad_option.output
    invalid = _invoke(KIND, "--topic", where, "-o", "size=99")
    assert invalid.exit_code == 1 and "Opciones no válidas" in invalid.output
    empty = create_topic(topic.vault, topic.subject, "Integrales").slug
    no_notes = _invoke(KIND, "--topic", f"{topic.subject}/{empty}")
    assert no_notes.exit_code == 1 and "aún no tiene apuntes" in no_notes.output
    assert fake.requests == []


def test_a_reached_cap_asks_for_the_flag(
    topic: ReviseTopic, fake: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_LLM__MAX_USD_PER_DAY", "0")
    reply_points(fake, *DEFAULT_POINTS)
    where = f"{topic.subject}/{topic.topic}"

    refused = _invoke(KIND, "--topic", where)
    assert refused.exit_code == 1 and "--confirm-over-cap" in refused.output

    confirmed = _invoke(KIND, "--topic", where, "--confirm-over-cap")
    assert confirmed.exit_code == 0, confirmed.output
