"""The secret guard: common key shapes are refused by name, ordinary content passes."""

from __future__ import annotations

import pytest
from secret_samples import SAMPLES

from studentassistant.vault import VaultError
from studentassistant.vault.secrets import SecretRefused, guard, looks_like_secret


@pytest.mark.parametrize(("pattern", "secret"), SAMPLES.items())
def test_each_common_key_shape_is_refused_by_its_pattern_name(pattern: str, secret: str) -> None:
    text = f"Apuntes del lunes. Clave: {secret} (no la compartas)"

    assert looks_like_secret(text) == pattern
    with pytest.raises(SecretRefused) as refusal:
        guard(text)

    assert pattern in str(refusal.value)
    assert secret not in str(refusal.value)
    assert refusal.value.pattern == pattern


@pytest.mark.parametrize("secret", SAMPLES.values())
def test_bytes_are_scanned_too(secret: str) -> None:
    with pytest.raises(SecretRefused):
        guard(b"\xff\xd8\xff\xe0" + secret.encode("ascii") + b"\x00\x01")


def test_ordinary_spanish_note_text_passes() -> None:
    text = (
        "Tema 3: Derivadas. La derivada de f en a es el límite del cociente incremental; "
        "si existe, f es derivable en a. Ejemplo: sk-ant es solo un prefijo, ghp_ también."
    )

    assert looks_like_secret(text) is None
    guard(text)


def test_a_jpeg_byte_prefix_passes() -> None:
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + bytes(
        range(256)
    )

    assert looks_like_secret(jpeg) is None
    guard(jpeg)


def test_the_refusal_is_a_vault_error() -> None:
    assert issubclass(SecretRefused, VaultError)
