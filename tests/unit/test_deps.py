from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, patch

from mystic.config import is_python_package_available


async def _run_inline(func: Callable[..., object], *args: object) -> object:
    return func(*args)


class TestIsPythonPackageAvailable:
    def test_returns_true_for_builtin(self) -> None:
        assert is_python_package_available("json") is True

    def test_returns_false_for_nonexistent(self) -> None:
        assert is_python_package_available("nonexistent_package_xyz_999") is False


class TestEnsurePythonExtra:
    async def test_skips_install_when_available(self) -> None:
        from mystic.config import ensure_python_extra

        with patch("mystic.config._pip_install") as mock_pip:
            await ensure_python_extra("json", "some-package")
            mock_pip.assert_not_called()

    async def test_calls_pip_when_missing(self) -> None:
        from mystic.config import ensure_python_extra

        with (
            patch("mystic.config.is_python_package_available", side_effect=[False, True]),
            patch("mystic.config._pip_install", return_value=0) as mock_pip,
            patch("asyncio.to_thread", new=AsyncMock(side_effect=_run_inline)),
        ):
            await ensure_python_extra("fake_pkg", "fake-pkg", label="Fake")
            mock_pip.assert_called_once_with("fake-pkg")

    async def test_raises_on_pip_failure(self) -> None:
        from mystic.config import ensure_python_extra

        import pytest

        with (
            patch("mystic.config.is_python_package_available", return_value=False),
            patch("mystic.config._pip_install", return_value=1),
            patch("asyncio.to_thread", new=AsyncMock(side_effect=_run_inline)),
            pytest.raises(RuntimeError, match="Failed to install"),
        ):
            await ensure_python_extra("fake_pkg", "fake-pkg")

    async def test_raises_when_still_not_importable_after_install(self) -> None:
        from mystic.config import ensure_python_extra

        import pytest

        with (
            patch("mystic.config.is_python_package_available", return_value=False),
            patch("mystic.config._pip_install", return_value=0),
            patch("asyncio.to_thread", new=AsyncMock(side_effect=_run_inline)),
            pytest.raises(RuntimeError, match="still not importable"),
        ):
            await ensure_python_extra("fake_pkg", "fake-pkg")
