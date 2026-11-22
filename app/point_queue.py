"""Shared point ledger and process-held claims for live-resizable course queues."""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from app.locking import file_lock


def point_key(point, job):
    identity = job.get('jobid')
    if not identity and job.get('objectid'):
        identity = f"chapter:{point['id']}:object:{job['objectid']}"
    if not identity and job.get('type') == 'empty':
        identity = 'chapter:' + str(point['id'])
    if not identity:
        raise ValueError('任务点缺少稳定编号，无法保证去重')
    return hashlib.sha256((str(job['type']) + ':' + str(identity)).encode()).hexdigest()[:24]


class PointLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.locks = self.path.with_suffix('.locks')
        self.locks.mkdir(exist_ok=True, mode=0o700)
        with self.db() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS points (
              id TEXT PRIMARY KEY, chapter_id TEXT, name TEXT, kind TEXT,
              status TEXT, owner TEXT, pid INTEGER, updated REAL, error TEXT)''')
            # Separate table keeps the nine-column ledger compatible with
            # already running workers from previous releases.
            connection.execute('''CREATE TABLE IF NOT EXISTS point_recovery (
              id TEXT PRIMARY KEY, failures INTEGER NOT NULL, next_retry REAL NOT NULL,
              blocked INTEGER NOT NULL, error TEXT NOT NULL)''')
            connection.execute('CREATE TABLE IF NOT EXISTS recovery_run (name TEXT PRIMARY KEY, value TEXT)')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, key):
        with self.db() as connection:
            row = connection.execute('SELECT * FROM points WHERE id=?', (key,)).fetchone()
            return dict(row) if row else None

    def ready(self, key, now=None):
        with self.db() as connection:
            row = connection.execute('SELECT * FROM point_recovery WHERE id=?', (key,)).fetchone()
        return not row or (not row['blocked'] and row['next_retry'] <= (time.time() if now is None else now))

    def recoveries(self, scope=None):
        with self.db() as connection:
            rows = connection.execute('''SELECT r.*,p.name,p.chapter_id FROM point_recovery r
                JOIN points p ON p.id=r.id WHERE p.status!='completed' ORDER BY r.next_retry''').fetchall()
        return [dict(row) for row in rows if scope is None or row['id'] in scope]

    def defer(self, key, owner, error, max_failures=6, delay=60, permanent=False, now=None):
        now = time.time() if now is None else now
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            point = connection.execute('SELECT status,owner FROM points WHERE id=?', (key,)).fetchone()
            if not point or point['status'] == 'completed' or point['owner'] != owner:
                return
            old = connection.execute('SELECT failures FROM point_recovery WHERE id=?', (key,)).fetchone()
            failures = (old['failures'] if old else 0) + 1
            blocked = permanent or failures >= max_failures
            due = now + (900 if blocked else min(delay * 2 ** (failures - 1), 900))
            connection.execute('''INSERT INTO point_recovery VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                failures=excluded.failures,next_retry=excluded.next_retry,blocked=excluded.blocked,error=excluded.error''',
                (key, failures, due, int(blocked), error))

    def reset_recoveries(self):
        with self.db() as connection:
            connection.execute('DELETE FROM point_recovery')

    def begin_task(self, task_id):
        if not task_id:
            return
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            old = connection.execute("SELECT value FROM recovery_run WHERE name='task' ").fetchone()
            if old and old['value'] != task_id:
                connection.execute('DELETE FROM point_recovery')
            connection.execute("INSERT OR REPLACE INTO recovery_run VALUES ('task',?)", (task_id,))

    def complete(self, key, point=None, job=None):
        with self.db() as connection:
            if point is not None:
                connection.execute('''INSERT OR IGNORE INTO points VALUES (?,?,?,?,?,?,?,?,?)''',
                    (key, str(point['id']), job.get('name', job['type']), job['type'], 'completed', '', None, time.time(), ''))
            connection.execute("UPDATE points SET status='completed',pid=NULL,updated=? WHERE id=?", (time.time(), key))
            connection.execute('DELETE FROM point_recovery WHERE id=?', (key,))

    def complete_chapters(self, chapter_ids):
        with self.db() as connection:
            for chapter_id in chapter_ids:
                connection.execute("UPDATE points SET status='completed',pid=NULL,updated=? WHERE chapter_id=?", (time.time(), str(chapter_id)))
                connection.execute('DELETE FROM point_recovery WHERE id IN (SELECT id FROM points WHERE chapter_id=?)', (str(chapter_id),))

    def claim(self, unit, owner):
        """Return an exclusive FD, inherited by the child from process creation."""
        key = unit['id']
        if os.name == 'nt':
            # Windows cannot pass the inherited descriptor used by POSIX builds.
            # A DB lease plus the worker PID gives the same single-owner guarantee.
            import psutil
            with self.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                old = connection.execute('SELECT status,pid,updated FROM points WHERE id=?', (key,)).fetchone()
                if old and old['status'] == 'completed':
                    return None
                if old and old['status'] == 'running':
                    alive = False
                    if old['pid']:
                        try:
                            process = psutil.Process(old['pid'])
                            alive = process.is_running() and process.create_time() <= old['updated'] + 120
                        except (psutil.Error, OSError):
                            pass
                    if alive or (old['pid'] is None and time.time() - old['updated'] < 120):
                        return None
                recovery = connection.execute('SELECT * FROM point_recovery WHERE id=?', (key,)).fetchone()
                if recovery and (recovery['blocked'] or recovery['next_retry'] > time.time()):
                    return None
                connection.execute('''INSERT INTO points VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    chapter_id=excluded.chapter_id,name=excluded.name,kind=excluded.kind,status='running',
                    owner=excluded.owner,pid=NULL,updated=excluded.updated,error='' ''',
                    (key, str(unit['point']['id']), unit['name'], unit['job']['type'], 'running', owner, None, time.time(), ''))
            return -1
        descriptor = os.open(self.locks / (key + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        try:
            with self.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                old = connection.execute('SELECT status FROM points WHERE id=?', (key,)).fetchone()
                if old and old['status'] == 'completed':
                    os.close(descriptor)
                    return None
                recovery = connection.execute('SELECT * FROM point_recovery WHERE id=?', (key,)).fetchone()
                if recovery and (recovery['blocked'] or recovery['next_retry'] > time.time()):
                    os.close(descriptor)
                    return None
                connection.execute('''INSERT INTO points VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    chapter_id=excluded.chapter_id,name=excluded.name,kind=excluded.kind,status='running',
                    owner=excluded.owner,pid=NULL,updated=excluded.updated,error='' ''',
                    (key, str(unit['point']['id']), unit['name'], unit['job']['type'], 'running', owner, None, time.time(), ''))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def update(self, key, owner, status, pid=None, error=''):
        with self.db() as connection:
            connection.execute("UPDATE points SET status=?,pid=?,error=?,updated=? WHERE id=? AND owner=? AND status!='completed'",
                               (status, pid, error, time.time(), key, owner))

    def renew(self, key, owner, pid):
        with self.db() as connection:
            connection.execute("UPDATE points SET pid=?,updated=? WHERE id=? AND owner=? AND status='running'",
                               (pid, time.time(), key, owner))


class RequestGate:
    """Spread learning API request starts across all workers of one course."""
    def __init__(self, path):
        self.path = Path(path)

    def wait(self, reporter, interval=0.35):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            reporter.checkpoint()
            try:
                with file_lock(self.path, blocking=False) as handle:
                    handle.seek(0)
                    try:
                        previous = float(handle.read() or 0)
                    except ValueError:
                        previous = 0
                    now = time.monotonic()
                    if previous > now + 3:
                        previous = now
                    target = max(now, previous + interval)
                    handle.seek(0)
                    handle.truncate()
                    handle.write(str(target).encode())
                    handle.flush()
                break
            except BlockingIOError:
                time.sleep(0.05)
        while time.monotonic() < target:
            reporter.checkpoint()
            time.sleep(min(0.05, max(0, target - time.monotonic())))
