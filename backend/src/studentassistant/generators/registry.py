"""The registry of generators: which `kind` names which `Generator`.

Each generator module registers its class on import (`@register` on `default_registry`), and
`studentassistant.generators` imports every built-in one, so importing the package is enough to
find them all. Tests build a `GeneratorRegistry` of their own.
"""

from __future__ import annotations

import re

from studentassistant.generators.base import Generator

KIND_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class UnknownGeneratorError(LookupError):
    """No generator is registered under that kind; the message is Spanish, for the student."""

    def __init__(self, kind: str, known: list[str]) -> None:
        listing = ", ".join(known) if known else "ninguno"
        super().__init__(f"No hay ningún generador «{kind}». Disponibles: {listing}.")
        self.kind = kind
        self.known = known


class GeneratorRegistry:
    """Kinds to generator classes; one instance of the class is made per `get`."""

    def __init__(self) -> None:
        self._generators: dict[str, type[Generator]] = {}

    def register[G: type[Generator]](self, generator: G) -> G:
        """Add `generator` under its `kind`; usable as a class decorator.

        Raises:
            ValueError: the kind is not a slug, or another class already has it.
        """
        kind = getattr(generator, "kind", "")
        if not isinstance(kind, str) or not KIND_PATTERN.fullmatch(kind):
            raise ValueError(f"{kind!r} is not a generator kind ([a-z0-9-], at most 40)")
        if not getattr(generator, "title", ""):
            raise ValueError(f"generator {kind!r} has no title")
        existing = self._generators.get(kind)
        if existing is not None and existing is not generator:
            raise ValueError(f"generator kind {kind!r} is already registered by {existing!r}")
        self._generators[kind] = generator
        return generator

    def unregister(self, kind: str) -> None:
        self._generators.pop(kind, None)

    def kinds(self) -> list[str]:
        """Every registered kind, sorted."""
        return sorted(self._generators)

    def classes(self) -> list[type[Generator]]:
        """Every registered generator class, sorted by kind."""
        return [self._generators[kind] for kind in self.kinds()]

    def __contains__(self, kind: object) -> bool:
        return kind in self._generators

    def lookup(self, kind: str) -> type[Generator] | None:
        """The class registered under `kind`, or `None`."""
        return self._generators.get(kind)

    def get(self, kind: str) -> Generator:
        """A fresh instance of the generator of `kind`. Raises `UnknownGeneratorError`."""
        generator = self._generators.get(kind)
        if generator is None:
            raise UnknownGeneratorError(kind, self.kinds())
        return generator()


default_registry = GeneratorRegistry()
register = default_registry.register
