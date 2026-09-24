"""Smoke tests: the src-layout package is importable and the pytest markers are wired."""

from importlib import metadata

import pytest

from studentassistant import __version__


def test_package_is_importable_and_reports_a_version() -> None:
    assert isinstance(__version__, str)
    assert __version__


@pytest.mark.integration
def test_installed_distribution_reports_the_same_version() -> None:
    """Needs a real installed distribution, so scripts/test.sh never selects it."""
    assert metadata.version("studentassistant") == __version__
