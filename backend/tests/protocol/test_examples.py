"""Each `protocol/<name>.schema.json` has a validating example and a round-tripping model.

The shared examples are the contract test (docs/modules/protocol.md): a new schema is picked up
here automatically, and fails until its example and its registered model exist.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from studentassistant.protocol import MODELS, model_for

PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
SCHEMA_SUFFIX = ".schema.json"
SCHEMA_PATHS = sorted(PROTOCOL_DIR.glob(f"*{SCHEMA_SUFFIX}"))


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
