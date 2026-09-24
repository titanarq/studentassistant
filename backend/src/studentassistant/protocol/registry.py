"""Maps each `protocol/<name>.schema.json` name to the model that parses it."""

from __future__ import annotations

from pydantic import BaseModel

from studentassistant.protocol.client import ClientHello

MODELS: dict[str, type[BaseModel]] = {
    "client.hello": ClientHello,
}


def model_for(name: str) -> type[BaseModel]:
    """The model for a schema name such as `client.hello`; `KeyError` if none is registered."""
    try:
        return MODELS[name]
    except KeyError:
        raise KeyError(f"no model registered for protocol message {name!r}") from None
