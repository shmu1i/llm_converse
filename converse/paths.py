import os
from pathlib import Path


def _data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "llm_converse"


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "llm_converse"


def db_path() -> Path:
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "converse.db"


def socket_path() -> Path:
    d = _runtime_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / "daemon.sock"


def pid_path() -> Path:
    return _runtime_dir() / "daemon.pid"


def lock_path() -> Path:
    d = _runtime_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / "daemon.lock"


def log_path() -> Path:
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "daemon.log"
