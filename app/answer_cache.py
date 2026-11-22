"""Reuse format-validated answers across recovery of the same course point."""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager


class AnswerCache:
    def __init__(self, path, config):
        self.path = path
        self.source = {key: config.get(key) for key in ('provider', 'fallbacks', 'endpoint', 'model',
            'siliconflow_endpoint', 'siliconflow_model', 'ai_vision', 'likeapi_vision', 'auto_ai_fallback')}
        with self.db() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS validated_answers (
              fingerprint TEXT PRIMARY KEY, answer TEXT NOT NULL, updated REAL NOT NULL)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS answer_recovery (
              fingerprint TEXT PRIMARY KEY, truncations INTEGER NOT NULL, error TEXT, updated REAL NOT NULL)''')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def key(self, question):
        content = {key: question.get(key) for key in ('id', 'type', 'title', 'options', 'blank_count', 'material_text')}
        content['images'] = question.get('image_data', [])
        content['choices'] = [{key: choice.get(key) for key in ('label', 'text', 'submit_value', 'content_key')} for choice in question.get('choices', [])]
        if not question.get('image_data'):
            content['image_sources'] = question.get('image_sources', [])
        encoded = json.dumps({'version': 2, 'source': self.source, 'question': content}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def get(self, question):
        with self.db() as connection:
            row = connection.execute('SELECT answer,updated FROM validated_answers WHERE fingerprint=?', (self.key(question),)).fetchone()
        if not row or time.time() - row[1] > 86400:
            return None
        return json.loads(row[0])

    def put(self, question, answer):
        with self.db() as connection:
            connection.execute('INSERT OR REPLACE INTO validated_answers VALUES (?,?,?)',
                               (self.key(question), json.dumps(answer, ensure_ascii=False), time.time()))
            connection.execute('DELETE FROM answer_recovery WHERE fingerprint=?', (self.key(question),))

    def hint(self, question):
        with self.db() as connection:
            row = connection.execute('SELECT truncations,error,updated FROM answer_recovery WHERE fingerprint=?', (self.key(question),)).fetchone()
        return (row[0], row[1]) if row and time.time() - row[2] <= 86400 else (0, '')

    def note_failure(self, question, truncated, error):
        with self.db() as connection:
            connection.execute('''INSERT INTO answer_recovery VALUES (?,?,?,?) ON CONFLICT(fingerprint)
                DO UPDATE SET truncations=answer_recovery.truncations+excluded.truncations,
                error=excluded.error,updated=excluded.updated''',
                (self.key(question), int(truncated), str(error)[:200], time.time()))
