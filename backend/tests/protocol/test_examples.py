"""Each `protocol/<name>.schema.json` has a validating example and a round-tripping model.

The shared examples are the contract test (docs/modules/protocol.md): a new schema is picked up
here automatically, and fails until its example and its registered model exist.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from studentassistant.protocol import MODELS, model_for, parse_client_event

PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
SCHEMA_SUFFIX = ".schema.json"
SCHEMA_PATHS = sorted(PROTOCOL_DIR.glob(f"*{SCHEMA_SUFFIX}"))
CLIENT_SCHEMA_PATHS = [path for path in SCHEMA_PATHS if path.name.startswith("client.")]


def _name(schema_path: Path) -> str:
    return schema_path.name.removesuffix(SCHEMA_SUFFIX)


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_protocol_dir_has_schemas() -> None:
    assert SCHEMA_PATHS, f"no *{SCHEMA_SUFFIX} under {PROTOCOL_DIR}"


def test_every_registered_model_has_a_schema() -> None:
    assert sorted(MODELS) == sorted(_name(path) for path in SCHEMA_PATHS)


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=_name)
def test_example_validates_and_round_trips(schema_path: Path) -> None:
    name = _name(schema_path)
    example_path = PROTOCOL_DIR / "examples" / f"{name}.json"
    assert example_path.is_file(), f"{name} has no example at {example_path}"

    schema = _load(schema_path)
    Draft202012Validator.check_schema(schema)
    example = _load(example_path)
    Draft202012Validator(schema).validate(example)

    model = model_for(name).model_validate(example)
    assert model.model_dump(mode="json", by_alias=True, exclude_none=True) == example


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=_name)
def test_schema_and_model_agree_on_top_level_fields(schema_path: Path) -> None:
    schema = _load(schema_path)
    model = model_for(_name(schema_path))
    fields = model.model_fields
    assert sorted(schema["properties"]) == sorted(fields)
    required = sorted(name for name, field in fields.items() if field.is_required())
    assert sorted(schema.get("required", [])) == required


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=_name)
def test_unknown_field_is_rejected_by_schema_and_model(schema_path: Path) -> None:
    name = _name(schema_path)
    example = _load(PROTOCOL_DIR / "examples" / f"{name}.json")
    assert isinstance(example, dict)
    tampered = {**example, "unexpected_field": 1}
    assert not Draft202012Validator(_load(schema_path)).is_valid(tampered)
    with pytest.raises(ValidationError):
        model_for(name).model_validate(tampered)


@pytest.mark.parametrize("schema_path", CLIENT_SCHEMA_PATHS, ids=_name)
def test_client_event_union_dispatches_on_type(schema_path: Path) -> None:
    name = _name(schema_path)
    example = _load(PROTOCOL_DIR / "examples" / f"{name}.json")
    event = parse_client_event(example)
    assert type(event) is model_for(name)
    assert event.model_dump(mode="json", by_alias=True, exclude_none=True) == example


@pytest.mark.parametrize("bad", [{"type": "transcript.client.draft"}, {"text": "sin tipo"}])
def test_client_event_union_rejects_unknown_or_missing_type(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_client_event(bad)
    assert excinfo.value.errors()[0]["type"] in {"union_tag_invalid", "union_tag_not_found"}


@pytest.mark.parametrize(
    "event",
    [
        {"type": "button", "button": "switch_source", "client_time_ms": 1},
        {"type": "button", "button": "pause", "source": "pdf", "client_time_ms": 1},
    ],
)
def test_button_source_goes_with_switch_source_only(event: dict[str, object]) -> None:
    schema = _load(PROTOCOL_DIR / "client.button.schema.json")
    assert not Draft202012Validator(schema).is_valid(event)
    with pytest.raises(ValidationError):
        parse_client_event(event)


def test_transcript_segment_cannot_end_before_it_starts() -> None:
    example = _load(PROTOCOL_DIR / "examples" / "client.transcript.client.final.json")
    assert isinstance(example, dict)
    with pytest.raises(ValidationError):
        parse_client_event({**example, "client_end_ms": example["client_start_ms"] - 1})
