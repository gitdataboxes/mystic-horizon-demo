"""Pytest fixtures for the Python refactor tests."""

from __future__ import annotations

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover - exercised only when pytest is absent.
    pytest = None

from tests.python_helpers import TempAppHome

if pytest is not None:

    @pytest.fixture()
    def app_home():
        with TempAppHome() as home:
            yield home
