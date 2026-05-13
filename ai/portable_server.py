"""
Portable llama.cpp ``llama-server`` subprocess (hidden window on Windows).

Paths are resolved relative to the plugin root (parent of the ``ai`` package).
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("wepawnai")

# Relative to plugin root (WepawnAI/)
BIN_SUBDIR = Path("bin")
MODEL_SUBDIR = Path("models")
SERVER_EXE_WIN = BIN_SUBDIR / "llama-server.exe"
SERVER_EXE_UNIX = BIN_SUBDIR / "llama-server"
DEFAULT_MODEL_NAME = "default.gguf"

DEFAULT_PREFERRED_PORT = 18081
PORT_PROBE_RANGE = 24
STARTUP_TCP_TIMEOUT_SEC = 45.0
STOP_TERMINATE_SEC = 8.0
STOP_KILL_SEC = 5.0


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _diag(msg: str) -> None:
    logger.warning(msg)
    print(f"[WepawnAI DIAG] {msg}", flush=True)


def _server_executable(root: Path) -> Path:
    if sys.platform == "win32":
        return root / SERVER_EXE_WIN
    return root / SERVER_EXE_UNIX


def _default_model_path(root: Path) -> Path:
    return root / MODEL_SUBDIR / DEFAULT_MODEL_NAME


def _pick_listen_port(preferred: int) -> int | None:
    """Return first port in ``preferred .. preferred+PORT_PROBE_RANGE`` that can bind on 127.0.0.1."""
    for port in range(preferred, preferred + PORT_PROBE_RANGE):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
        except OSError as exc:
            _diag(f"Port bind probe failed for {port}: {exc}")
            continue
        return port
    _diag(f"No free port in range {preferred}-{preferred + PORT_PROBE_RANGE - 1} on 127.0.0.1")
    return None


def _wait_for_tcp(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                _diag(f"TCP accept on 127.0.0.1:{port} (llama-server ready)")
                return True
        except OSError:
            time.sleep(0.25)
    _diag(f"Timeout waiting for llama-server TCP on port {port}")
    return False


class PortableLlamaServer:
    """
    Manages a single ``llama-server`` child process for this MO2 session.

    Executable: ``<plugin>/bin/llama-server(.exe)``
    Model: ``<plugin>/models/default.gguf`` (replace with your GGUF).
    """

    __slots__ = ("_root", "_preferred_port", "_actual_port", "_proc")

    def __init__(self, root: Path | None = None, *, preferred_port: int = DEFAULT_PREFERRED_PORT) -> None:
        self._root = root or plugin_root()
        self._preferred_port = int(preferred_port)
        self._actual_port = self._preferred_port
        self._proc: subprocess.Popen | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def actual_port(self) -> int:
        return self._actual_port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._actual_port}"

    def is_running(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def start(self) -> bool:
        if self.is_running():
            _diag("PortableLlamaServer.start: already running (PID reuse skipped)")
            return True

        exe = _server_executable(self._root)
        model = _default_model_path(self._root)
        if not exe.is_file():
            try:
                er = exe.relative_to(self._root)
            except ValueError:
                er = exe
            _diag(f"llama-server executable missing (expected under plugin): {er}")
            return False
        if not model.is_file():
            try:
                mr = model.relative_to(self._root)
            except ValueError:
                mr = model
            _diag(f"GGUF model missing (expected under plugin): {mr}")
            return False

        port = _pick_listen_port(self._preferred_port)
        if port is None:
            return False
        self._actual_port = port
        _diag(
            f"Starting llama-server exe={exe.name} model={model.name} "
            f"port={port} (plugin-relative paths under {self._root.name})"
        )

        args = [
            str(exe),
            "-m",
            str(model),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]

        creationflags = 0
        startupinfo = None
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # CREATE_NO_WINDOW = 0x08000000 on Windows
            if creationflags == 0:
                creationflags = 0x08000000
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            self._proc = subprocess.Popen(
                args,
                cwd=str(self._root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        except OSError as exc:
            _diag(f"Popen failed for llama-server: {exc}")
            self._proc = None
            return False

        pid = self._proc.pid
        _diag(f"llama-server subprocess started PID={pid} listening on 127.0.0.1:{port}")

        if not _wait_for_tcp(port, STARTUP_TCP_TIMEOUT_SEC):
            _diag(f"Startup TCP wait failed; terminating PID={pid}")
            self.stop()
            return False

        return True

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            _diag("PortableLlamaServer.stop: no active subprocess")
            return

        pid = proc.pid
        _diag(f"Graceful shutdown: terminate() PID={pid}")
        try:
            proc.terminate()
        except OSError as exc:
            _diag(f"terminate() failed for PID={pid}: {exc}")

        try:
            proc.wait(timeout=STOP_TERMINATE_SEC)
            _diag(f"Subprocess PID={pid} exited cleanly after terminate()")
        except subprocess.TimeoutExpired:
            _diag(f"PID={pid} did not exit within {STOP_TERMINATE_SEC}s; kill()")
            try:
                proc.kill()
            except OSError as exc:
                _diag(f"kill() failed for PID={pid}: {exc}")
            try:
                proc.wait(timeout=STOP_KILL_SEC)
            except subprocess.TimeoutExpired:
                _diag(f"PID={pid} still alive after kill(); orphan risk — check Task Manager")
            else:
                _diag(f"Subprocess PID={pid} released after kill()")

        self._proc = None
