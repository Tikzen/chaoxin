"""An explicit, durable allowance for one additional submission per work."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


class RedoPermits:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS quiz_redo_permits (
                jobid TEXT PRIMARY KEY, baseline INTEGER NOT NULL, consumed INTEGER NOT NULL,
                granted TEXT NOT NULL, reason TEXT NOT NULL)''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def grant_once(self, jobid, baseline, reason):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO quiz_redo_permits VALUES (?,?,0,?,?)',
                       (jobid, baseline, datetime.now(timezone.utc).isoformat(), reason))

    def available(self, jobid, baseline):
        with self.db() as db:
            row = db.execute('SELECT baseline,consumed FROM quiz_redo_permits WHERE jobid=?', (jobid,)).fetchone()
        return bool(row and row == (baseline, 0))

    def consume(self, jobid, baseline):
        with self.db() as db:
            return db.execute('UPDATE quiz_redo_permits SET consumed=1 WHERE jobid=? AND baseline=? AND consumed=0',
                              (jobid, baseline)).rowcount == 1
