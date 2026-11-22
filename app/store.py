import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from app.runtime import data_root, resource_root

ROOT = resource_root()
DATA = data_root()


def now():
    return datetime.now(timezone.utc).isoformat()


def private_file(path, value):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as file:
        file.write(value)


def initialize():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        DATA.chmod(0o700)
    (DATA / "jobs").mkdir(exist_ok=True)
    private_file(DATA / "master.key", Fernet.generate_key().decode())
    private_file(DATA / "access-token", secrets.token_urlsafe(24))
    with db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, hint TEXT NOT NULL,
            fingerprint TEXT UNIQUE NOT NULL, credentials TEXT NOT NULL,
            mode TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS courses (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            title TEXT NOT NULL, teacher TEXT NOT NULL, raw TEXT NOT NULL,
            total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
            synced_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
            course_id TEXT NOT NULL REFERENCES courses(id), title TEXT NOT NULL,
            engine TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
            state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(courses)")}
        for name, definition in {
            "source_position": "INTEGER NOT NULL DEFAULT 0",
            "progress_json": "TEXT NOT NULL DEFAULT '{}'",
            "progress_updated_at": "TEXT",
            "progress_status": "TEXT NOT NULL DEFAULT 'idle'",
            "progress_error": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE courses ADD COLUMN {name} {definition}")
        if "source_position" not in columns:
            # Original imports were inserted in platform order, before the UI sorted by title.
            conn.execute("UPDATE courses SET source_position=rowid")


def public_course(course):
    result = {key: course[key] for key in ("id", "account_id", "title", "teacher", "total", "done", "synced_at",
        "source_position", "progress_updated_at", "progress_status", "progress_error")}
    snapshot = json.loads(course["progress_json"])
    result.update(task_total=snapshot.get("task_total"), task_done=snapshot.get("task_done"),
                  tasks_complete=snapshot.get("tasks_complete", False))
    return result


@contextmanager
def runtime_lock():
    """Reject a second server sharing this DB, including accidental double clicks."""
    handle = (DATA / "server.lock").open("a+b")
    if handle.seek(0, 2) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("此数据目录已有控制台运行，请使用已打开的窗口，不要重复启动") from None
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def db():
    conn = sqlite3.connect(DATA / "study.db", timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows(sql, args=()):
    with db() as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def one(sql, args=()):
    result = rows(sql, args)
    return result[0] if result else None


def execute(sql, args=()):
    with db() as conn:
        conn.execute(sql, args)


def cipher():
    return Fernet((DATA / "master.key").read_bytes().strip())


def seal(value):
    return cipher().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def unseal(value):
    return json.loads(cipher().decrypt(value.encode()))


def token():
    return os.environ.get("STUDY_ACCESS_TOKEN") or (DATA / "access-token").read_text().strip()


def session_token():
    return hashlib.sha256((token() + (DATA / "master.key").read_text()).encode()).hexdigest()


def setting(key, default=None):
    record = one("SELECT value FROM settings WHERE key=?", (key,))
    return unseal(record["value"]) if record else default


def set_setting(key, value):
    execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, seal(value)))


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
