"""Synchronous Unix-socket client. Reused by the CLI."""

import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

from . import paths, protocol


class DaemonError(RuntimeError):
    pass


def _connect(timeout: float = 2.0) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(paths.socket_path()))
    return s


def ensure_daemon(timeout: float = 5.0) -> None:
    """Make sure a daemon is reachable. Spawn one if not.

    Never unlinks the socket itself: the daemon's serve() handles stale
    socket files on the spawn path, and unlinking a path bound to a
    healthy-but-busy daemon would break new connects to it. Spawning a
    duplicate is safe — run() holds an exclusive flock so duplicates
    exit immediately.
    """
    sock = paths.socket_path()
    deadline = time.monotonic() + timeout
    spawned = False
    while time.monotonic() < deadline:
        if sock.exists():
            try:
                with _connect(timeout=1.0) as s:
                    s.sendall(protocol.encode({"op": protocol.OP_PING}))
                    _readline(s)
                return
            except (ConnectionRefusedError, FileNotFoundError):
                pass  # nothing listening — fall through to spawn
            except (OSError, socket.timeout):
                time.sleep(0.05)
                continue  # busy / transient — retry the ping
        if not spawned:
            log = open(paths.log_path(), "ab")
            subprocess.Popen(
                [sys.executable, "-m", "converse", "--daemon"],
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                env=os.environ.copy(),
            )
            spawned = True
        time.sleep(0.05)
    raise DaemonError("daemon failed to start (see logs at %s)" % paths.log_path())


def _readline(sock: socket.socket) -> bytes:
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in chunk:
            # may have trailing data, but the server sends one line then closes
            # for non-streaming ops; tail uses iter_lines instead
            break
    nl = buf.find(b"\n")
    return bytes(buf[: nl if nl >= 0 else len(buf)])


def request(req: dict) -> dict:
    ensure_daemon()
    with _connect() as s:
        s.sendall(protocol.encode(req))
        line = _readline(s)
    if not line:
        raise DaemonError("empty response from daemon")
    resp = protocol.decode(line)
    if "error" in resp:
        raise DaemonError(resp["error"])
    return resp


def stream(req: dict) -> Iterator[dict]:
    """Send a streaming request (e.g. tail) and yield each JSON line until disconnect."""
    ensure_daemon()
    s = _connect(timeout=None)
    try:
        s.sendall(protocol.encode(req))
        buf = bytearray()
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                if not line:
                    continue
                obj = protocol.decode(line)
                if "error" in obj:
                    raise DaemonError(obj["error"])
                yield obj
    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()


def stop_daemon(timeout: float = 5.0) -> bool:
    """Send SIGTERM to the running daemon and wait for it to actually exit.

    Without the wait, an immediate ensure_daemon() can race the dying
    daemon: socket may still exist, lock not yet released, listener
    half-closed. Polling pid liveness keeps the stop -> restart sequence
    deterministic.
    """
    pid_file = paths.pid_path()
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        with contextlib.suppress(FileNotFoundError):
            pid_file.unlink()
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return True
