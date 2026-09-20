import json
import sqlite3
import threading
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
          CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY, session_date TEXT NOT NULL, mode TEXT NOT NULL,
            received_at TEXT NOT NULL, stage TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS batch_session ON batches(session_date,mode);
          CREATE TABLE IF NOT EXISTS reports (
            date TEXT NOT NULL, mode TEXT NOT NULL, generated_at TEXT NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(date,mode));
          CREATE TABLE IF NOT EXISTS research_manifests (
            date TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS research_decisions (
            date TEXT NOT NULL, checkpoint TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(date,checkpoint));
        ''')
        self.conn.commit()

    def batch(self, date, mode, received_at, stage, data):
        with self.lock, self.conn:
            self.conn.execute('INSERT INTO batches(session_date,mode,received_at,stage,payload) VALUES(?,?,?,?,?)',
                              (date, mode, received_at, stage, json.dumps(data, ensure_ascii=False, allow_nan=False)))

    def batches(self, date, mode='live'):
        with self.lock:
            rows = self.conn.execute('SELECT received_at,stage,payload FROM batches WHERE session_date=? AND mode=? ORDER BY id', (date, mode)).fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def report(self, date, mode, data):
        with self.lock, self.conn:
            self.conn.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?,?)',
                              (date, mode, data.get('generated_at', ''), json.dumps(data, ensure_ascii=False, allow_nan=False)))

    def latest_report(self, mode='live'):
        with self.lock:
            row = self.conn.execute('SELECT payload FROM reports WHERE mode=? ORDER BY date DESC LIMIT 1', (mode,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_report(self, date, mode='live'):
        with self.lock:
            row = self.conn.execute('SELECT payload FROM reports WHERE date=? AND mode=?', (date, mode)).fetchone()
        return json.loads(row[0]) if row else None

    def list_reports(self, mode='live', limit=365):
        # Listing dates does not decode every multi-megabyte raw report.
        with self.lock:
            rows = self.conn.execute('SELECT date,mode,generated_at FROM reports WHERE mode=? ORDER BY date DESC LIMIT ?',
                                     (mode, max(1, min(365, int(limit))))).fetchall()
        return [dict(date=row[0], mode=row[1], generated_at=row[2]) for row in rows]

    def batch_dates(self, mode='live', limit=120):
        with self.lock:
            rows = self.conn.execute('SELECT session_date,COUNT(*) FROM batches WHERE mode=? GROUP BY session_date ORDER BY session_date DESC LIMIT ?',
                                     (mode, max(1, min(120, int(limit))))).fetchall()
        return [dict(date=day, batch_count=count) for day, count in rows]

    def freeze_manifest(self, manifest):
        """First preparation is immutable: later outcomes cannot rewrite context."""
        with self.lock, self.conn:
            self.conn.execute('INSERT OR IGNORE INTO research_manifests VALUES(?,?)',
                              (manifest['date'], json.dumps(manifest, ensure_ascii=False, allow_nan=False)))

    def manifest(self, date):
        with self.lock:
            row = self.conn.execute('SELECT payload FROM research_manifests WHERE date=?', (date,)).fetchone()
        return json.loads(row[0]) if row else None

    def freeze_decision(self, date, checkpoint, payload):
        with self.lock, self.conn:
            self.conn.execute('INSERT OR IGNORE INTO research_decisions VALUES(?,?,?)',
                              (date,checkpoint,json.dumps(payload,ensure_ascii=False,allow_nan=False)))

    def decision(self, date, checkpoint):
        with self.lock:
            row = self.conn.execute('SELECT payload FROM research_decisions WHERE date=? AND checkpoint=?',
                                    (date,checkpoint)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        with self.lock:
            self.conn.close()
