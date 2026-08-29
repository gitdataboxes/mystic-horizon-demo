from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mystic.config import LiveKitConfig
from mystic.livekit import (
    LIVEKIT_CHECKSUMS,
    ResolvedLiveKitBinary,
    _ensure_macos_livekit_binary,
    _verify_checksum,
    ensure_managed_livekit_symlink,
    ensure_livekit_binary,
    get_livekit_binary_version,
    resolve_livekit_binary_path,
    start_livekit_server,
    stop_livekit_server,
    validate_livekit_version,
    wait_for_livekit_server,
)

CONFIG = LiveKitConfig(
    host="127.0.0.1",
    port=7880,
    apiKey="APIdeadbeef",
    apiSecret="secret-value",
)


class FakeProcess:
    def __init__(
        self,
        *,
        exit_code: int | None = None,
        wait_side_effect: BaseException | None = None,
    ) -> None:
        self.stdout = io.BytesIO()
        self.stderr = None
        self._exit_code = exit_code
        self._wait_side_effect = wait_side_effect
        self.terminated = False
        self.killed = False
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        if self._wait_side_effect is None:
            self._exit_code = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self._wait_side_effect is not None:
            raise self._wait_side_effect
        if self._exit_code is None:
            self._exit_code = 0
        return self._exit_code

    def kill(self) -> None:
        self.killed = True
        self._wait_side_effect = None
        self._exit_code = -9


async def _run_inline(func: Callable[..., object], *args: object) -> object:
    return func(*args)


class LiveKitServerTests(unittest.TestCase):
    @patch(
        "mystic.livekit.resolve_supported_livekit_binary",
        return_value=ResolvedLiveKitBinary(Path("/tmp/livekit-server"), "1.9.12"),
    )
    @patch("mystic.livekit.subprocess.Popen")
    @patch("mystic.livekit.subprocess.run")
    def test_start_livekit_server_passes_key_secret_pair_with_required_spacing(
        self,
        run_mock: Mock,
        popen_mock: Mock,
        _resolve_binary_mock: Mock,
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(args=["lsof"], returncode=1, stdout="", stderr="")
        popen_mock.return_value = FakeProcess()

        start_livekit_server(CONFIG)

        args = list(popen_mock.call_args.args[0])
        keys_index = args.index("--keys")
        self.assertEqual(args[keys_index + 1], "APIdeadbeef: secret-value")
        # RTC ports derived from HTTP port
        tcp_index = args.index("--rtc.tcp_port")
        self.assertEqual(args[tcp_index + 1], "7881")
        udp_index = args.index("--udp-port")
        self.assertEqual(args[udp_index + 1], "7882")

    @patch(
        "mystic.livekit.resolve_supported_livekit_binary",
        return_value=ResolvedLiveKitBinary(Path("/tmp/livekit-server"), "1.9.12"),
    )
    @patch("mystic.livekit.subprocess.Popen")
    @patch("mystic.livekit.subprocess.run")
    def test_start_livekit_server_trims_whitespace_from_keys(
        self,
        run_mock: Mock,
        popen_mock: Mock,
        _resolve_binary_mock: Mock,
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(args=["lsof"], returncode=1, stdout="", stderr="")
        popen_mock.return_value = FakeProcess()
        config = LiveKitConfig(
            host="127.0.0.1",
            port=7880,
            apiKey=" APIdeadbeef ",
            apiSecret=" secret-value ",
        )

        start_livekit_server(config)

        args = list(popen_mock.call_args.args[0])
        keys_index = args.index("--keys")
        self.assertEqual(args[keys_index + 1], "APIdeadbeef: secret-value")

    @patch(
        "mystic.livekit.resolve_supported_livekit_binary",
        return_value=ResolvedLiveKitBinary(Path("/tmp/livekit-server"), "1.9.12"),
    )
    @patch("mystic.livekit.subprocess.Popen")
    @patch("mystic.livekit.subprocess.run")
    @patch("mystic.livekit.os.kill")
    @patch("mystic.livekit.time.sleep")
    def test_start_livekit_server_kills_orphaned_livekit_server(
        self,
        _sleep_mock: Mock,
        kill_mock: Mock,
        run_mock: Mock,
        popen_mock: Mock,
        _resolve_binary_mock: Mock,
    ) -> None:
        import signal

        run_mock.side_effect = [
            # First get_listening_process — lsof finds orphan
            subprocess.CompletedProcess(args=["lsof"], returncode=0, stdout="1234\n", stderr=""),
            # First get_listening_process — ps identifies it as livekit-server
            subprocess.CompletedProcess(
                args=["ps"],
                returncode=0,
                stdout="/usr/local/bin/livekit-server --dev --port 7880\n",
                stderr="",
            ),
            # Retry get_listening_process after kill — port is now free
            subprocess.CompletedProcess(args=["lsof"], returncode=1, stdout="", stderr=""),
        ]
        popen_mock.return_value = FakeProcess()

        start_livekit_server(CONFIG)

        kill_mock.assert_called_once_with(1234, signal.SIGKILL)
        popen_mock.assert_called_once()

    @patch("mystic.livekit.resolve_supported_livekit_binary", return_value=None)
    @patch("mystic.livekit.subprocess.run")
    def test_start_livekit_server_raises_when_binary_is_missing(
        self,
        run_mock: Mock,
        _resolve_binary_mock: Mock,
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(args=["lsof"], returncode=1, stdout="", stderr="")

        with self.assertRaisesRegex(RuntimeError, "livekit-server not found"):
            start_livekit_server(CONFIG)

    @patch(
        "mystic.livekit.resolve_supported_livekit_binary",
        side_effect=RuntimeError("Unsupported livekit-server at /usr/local/bin/livekit-server"),
    )
    @patch("mystic.livekit.subprocess.Popen")
    @patch("mystic.livekit.subprocess.run")
    def test_start_livekit_server_rejects_unsupported_binary(
        self,
        run_mock: Mock,
        popen_mock: Mock,
        _resolve_binary_mock: Mock,
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(args=["lsof"], returncode=1, stdout="", stderr="")

        with self.assertRaisesRegex(RuntimeError, "Unsupported livekit-server"):
            start_livekit_server(CONFIG)

        popen_mock.assert_not_called()

    def test_stop_livekit_server_waits_for_exit(self) -> None:
        proc = FakeProcess()

        stop_livekit_server(proc)

        self.assertTrue(proc.terminated)
        self.assertFalse(proc.killed)
        self.assertEqual(proc.wait_calls, [2.0])

    def test_stop_livekit_server_kills_stuck_process(self) -> None:
        proc = FakeProcess(
            wait_side_effect=subprocess.TimeoutExpired(cmd=["livekit-server"], timeout=2.0)
        )

        stop_livekit_server(proc)

        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertEqual(proc.wait_calls, [2.0, 1.0])


class LiveKitBinaryResolutionTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_livekit_binary_path_prefers_path_over_managed_binary(self) -> None:
        managed = Path("/tmp/managed-livekit-server")
        with (
            patch("mystic.livekit.shutil.which", return_value="/usr/local/bin/livekit-server"),
            patch("mystic.livekit.get_binary_path", return_value=managed),
        ):
            resolved = resolve_livekit_binary_path()
        self.assertEqual(resolved, Path("/usr/local/bin/livekit-server"))

    def test_resolve_livekit_binary_path_falls_back_to_managed_binary(self) -> None:
        managed = Path("/tmp/managed-livekit-server")
        with (
            patch("mystic.livekit.shutil.which", return_value=None),
            patch("mystic.livekit.get_binary_path", return_value=managed),
            patch.object(Path, "exists", return_value=True),
        ):
            resolved = resolve_livekit_binary_path()
        self.assertEqual(resolved, managed)

    async def test_ensure_livekit_binary_returns_existing_binary_on_path(self) -> None:
        with patch(
            "mystic.livekit.get_platform_system",
            return_value="Linux",
        ), patch(
            "mystic.livekit.resolve_supported_livekit_binary",
            return_value=ResolvedLiveKitBinary(Path("/usr/local/bin/livekit-server"), "1.9.12"),
        ), patch(
            "mystic.livekit.asyncio.to_thread",
            new=AsyncMock(side_effect=_run_inline),
        ):
            result = await ensure_livekit_binary()
        self.assertEqual(result, "/usr/local/bin/livekit-server")

    async def test_ensure_livekit_binary_rejects_unsupported_binary_on_path(self) -> None:
        with (
            patch("mystic.livekit.get_platform_system", return_value="Linux"),
            patch(
                "mystic.livekit.resolve_supported_livekit_binary",
                side_effect=RuntimeError("Unsupported livekit-server at /usr/local/bin/livekit-server"),
            ),
            patch(
                "mystic.livekit.resolve_livekit_binary_path",
                return_value=Path("/usr/local/bin/livekit-server"),
            ),
            patch("mystic.livekit.asyncio.to_thread", new=AsyncMock(side_effect=_run_inline)),
        ):
            with self.assertRaisesRegex(RuntimeError, "Unsupported livekit-server"):
                await ensure_livekit_binary()

    async def test_ensure_livekit_binary_downloads_managed_binary_on_linux(self) -> None:
        with (
            patch("mystic.livekit.get_platform_system", return_value="Linux"),
            patch("mystic.livekit.get_binary_path", return_value=Path("/tmp/livekit-server")),
            patch("mystic.livekit.get_download_url", return_value="https://example.invalid/livekit.tar.gz"),
            patch("mystic.livekit.resolve_supported_livekit_binary", return_value=None),
            patch("mystic.livekit._download_binary"),
            patch("mystic.livekit.validate_livekit_binary", return_value="1.7.2"),
            patch("mystic.livekit.logger"),
            patch("mystic.livekit.asyncio.to_thread", new=AsyncMock(side_effect=_run_inline)),
        ):
            result = await ensure_livekit_binary()
        self.assertEqual(result, "/tmp/livekit-server")

    async def test_ensure_livekit_binary_delegates_to_macos_worker(self) -> None:
        to_thread_mock = AsyncMock(return_value="/tmp/livekit-server")
        with (
            patch("mystic.livekit.get_platform_system", return_value="Darwin"),
            patch("mystic.livekit.asyncio.to_thread", new=to_thread_mock),
        ):
            result = await ensure_livekit_binary()
        self.assertEqual(result, "/tmp/livekit-server")
        to_thread_mock.assert_awaited_once_with(_ensure_macos_livekit_binary)

    async def test_ensure_livekit_binary_redownloads_invalid_managed_binary_on_linux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            managed = tmp_path / "bin" / "livekit-server"
            managed.parent.mkdir(parents=True, exist_ok=True)
            managed.write_text("bad-binary", encoding="utf-8")
            with (
                patch("mystic.livekit.get_platform_system", return_value="Linux"),
                patch("mystic.livekit.get_binary_path", return_value=managed),
                patch(
                    "mystic.livekit.resolve_supported_livekit_binary",
                    side_effect=RuntimeError("Unsupported livekit-server at /tmp/livekit-server"),
                ),
                patch("mystic.livekit.resolve_livekit_binary_path", return_value=managed),
                patch("mystic.livekit.get_download_url", return_value="https://example.invalid/livekit.tar.gz"),
                patch("mystic.livekit._download_binary"),
                patch("mystic.livekit.validate_livekit_binary", return_value="1.7.2"),
                patch("mystic.livekit.logger"),
                patch("mystic.livekit.asyncio.to_thread", new=AsyncMock(side_effect=_run_inline)),
            ):
                result = await ensure_livekit_binary()
        self.assertEqual(result, str(managed))

    def test_ensure_macos_livekit_binary_raises_homebrew_instruction(self) -> None:
        with (
            patch("mystic.livekit.get_system_binary_path", return_value=None),
            patch("mystic.livekit.get_binary_path", return_value=Path("/tmp/livekit-server")),
            patch("mystic.livekit.get_brew_path", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "Homebrew is required"):
                _ensure_macos_livekit_binary()

    def test_ensure_macos_livekit_binary_installs_and_links_homebrew_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            managed = tmp_path / "bin" / "livekit-server"
            source = tmp_path / "opt" / "homebrew" / "bin" / "livekit-server"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("binary", encoding="utf-8")
            with (
                patch("mystic.livekit.get_system_binary_path", return_value=None),
                patch("mystic.livekit.get_binary_path", return_value=managed),
                patch("mystic.livekit.install_livekit_with_homebrew"),
                patch("mystic.livekit.find_brew_livekit_binary", return_value=source),
                patch("mystic.livekit.validate_livekit_binary", return_value="1.9.12"),
                patch("mystic.livekit.ensure_managed_livekit_symlink", return_value=managed),
            ):
                result = _ensure_macos_livekit_binary()
        self.assertEqual(result, str(managed))


class LiveKitVersionTests(unittest.TestCase):
    def test_get_livekit_binary_version_parses_semver_from_output(self) -> None:
        with patch(
            "mystic.livekit.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["livekit-server", "--version"],
                returncode=0,
                stdout="livekit-server version 1.9.12\n",
                stderr="",
            ),
        ):
            version = get_livekit_binary_version(Path("/usr/local/bin/livekit-server"))
        self.assertEqual(version, "1.9.12")

    def test_validate_livekit_version_accepts_newer_same_major(self) -> None:
        validate_livekit_version("1.9.12")

    def test_validate_livekit_version_rejects_older_release(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "older than the minimum supported version"):
            validate_livekit_version("1.6.9")

    def test_validate_livekit_version_rejects_major_mismatch(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "supports LiveKit 1.x"):
            validate_livekit_version("2.0.0")

    def test_ensure_managed_livekit_symlink_creates_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "livekit-server-source"
            source.write_text("binary", encoding="utf-8")
            managed = tmp_path / "bin" / "livekit-server"
            with patch("mystic.livekit.get_binary_path", return_value=managed):
                created = ensure_managed_livekit_symlink(source)
            self.assertEqual(created, managed)
            self.assertTrue(managed.is_symlink())
            self.assertEqual(managed.resolve(), source.resolve())


class LiveKitChecksumTests(unittest.TestCase):
    def test_verify_checksum_passes_for_known_platform(self) -> None:
        data = b"fake archive content"
        digest = hashlib.sha256(data).hexdigest()
        with patch("mystic.livekit.get_platform_arch", return_value=("linux", "amd64")):
            with patch.dict(LIVEKIT_CHECKSUMS, {"linux_amd64": digest}):
                _verify_checksum(data)  # should not raise

    def test_verify_checksum_raises_on_mismatch(self) -> None:
        data = b"fake archive content"
        with patch("mystic.livekit.get_platform_arch", return_value=("linux", "amd64")):
            with patch.dict(LIVEKIT_CHECKSUMS, {"linux_amd64": "bad" * 16}):
                with self.assertRaisesRegex(RuntimeError, "Checksum mismatch"):
                    _verify_checksum(data)

    def test_verify_checksum_skips_unknown_platform(self) -> None:
        with patch("mystic.livekit.get_platform_arch", return_value=("freebsd", "amd64")):
            _verify_checksum(b"anything")  # should not raise


class LiveKitWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_livekit_server_raises_if_process_exits_early(self) -> None:
        process = FakeProcess(exit_code=1)

        with patch("mystic.livekit.is_tcp_reachable", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "livekit-server exited before startup"):
                await wait_for_livekit_server(CONFIG, process, timeout_ms=100)


if __name__ == "__main__":
    unittest.main()
