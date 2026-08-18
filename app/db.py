"""SQLite 연결/초기화/백업."""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import config

_initialised = False


def _connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: Path | None = None) -> None:
    """스키마를 적용한다. 여러 번 호출해도 안전하다."""
    global _initialised
    config.ensure_dirs()
    sql = config.SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _connect(path)
    try:
        conn.executescript(sql)
        _seed_settings(conn)
    finally:
        conn.close()
    _initialised = True


def _seed_settings(conn: sqlite3.Connection) -> None:
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            (key, value),
        )


@contextmanager
def get_conn(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """읽기 전용 또는 단발성 작업용 커넥션."""
    if not _initialised:
        init_db(path)
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """쓰기 트랜잭션. 예외가 나면 전부 롤백된다."""
    if not _initialised:
        init_db(path)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(sql, tuple(params)).fetchone()


def scalar(sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    row = query_one(sql, params)
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


# ---------------------------------------------------------------- settings
def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else config.DEFAULT_SETTINGS.get(key, default)


def get_settings() -> dict[str, str]:
    data = dict(config.DEFAULT_SETTINGS)
    for row in query("SELECT key, value FROM settings"):
        data[row["key"]] = row["value"]
    return data


def set_settings(values: dict[str, str]) -> None:
    with tx() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


# ---------------------------------------------------------------- backup
def backup(tag: str = "auto") -> Path:
    """DB 파일 스냅샷을 남긴다. 되돌릴 수 없는 작업 전에 호출한다."""
    config.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = config.BACKUP_DIR / f"stock_{stamp}_{tag}.db"
    if config.DB_PATH.exists():
        src = _connect()
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)          # WAL 이 있어도 안전한 온라인 백업
            finally:
                dst.close()
        finally:
            src.close()
    prune_backups()
    return dest


def prune_backups() -> None:
    keep = int(get_setting("backup_keep", "30") or 30)
    files = sorted(config.BACKUP_DIR.glob("stock_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def restore(backup_path: Path) -> None:
    backup(tag="before_restore")
    shutil.copy2(backup_path, config.DB_PATH)
    for suffix in ("-wal", "-shm"):
        stale = Path(str(config.DB_PATH) + suffix)
        if stale.exists():
            stale.unlink()
