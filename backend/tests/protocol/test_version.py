"""`protocol_version` negotiation: same MAJOR interoperates, a different MAJOR is refused."""

import pytest
from pydantic import ValidationError

from studentassistant.protocol import (
    PROTOCOL_VERSION,
    ClientHello,
    IncompatibleProtocolVersionError,
    check_compatible,
    negotiate,
    parse_version,
)


def test_own_version_is_major_minor() -> None:
    assert parse_version(PROTOCOL_VERSION) == (1, 1)


@pytest.mark.parametrize("bad", ["1", "1.0.0", "v1.0", "01.0", "1.x", ""])
def test_malformed_version_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="malformed protocol_version"):
        parse_version(bad)


def test_same_major_negotiates_the_lower_minor() -> None:
    assert negotiate("1.3", ours="1.1") == "1.1"
    assert negotiate("1.0", ours="1.2") == "1.0"
    assert negotiate("1.10", ours="1.9") == "1.9"


def test_different_major_is_refused_with_both_versions() -> None:
    with pytest.raises(IncompatibleProtocolVersionError) as excinfo:
        check_compatible("2.0", ours="1.4")

    message = str(excinfo.value)
    assert "2.0" in message
    assert "1.4" in message
    assert excinfo.value.peer == "2.0"


def test_hello_rejects_a_malformed_version() -> None:
    with pytest.raises(ValidationError):
        ClientHello.model_validate(
            {
                "type": "hello",
                "protocol_version": "one",
                "capabilities": {"stt": "client", "stt_provider": "web-speech"},
                "client_time_ms": 0,
            }
        )
