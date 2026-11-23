from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .models import Collection, SearchQuery, Vacancy, Window

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    query_key TEXT NOT NULL,
    window_from TEXT NOT NULL,
    window_to TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    tracks_cursor INTEGER NOT NULL,
    summary TEXT,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_query ON runs(query_key, status, window_to);
CREATE TABLE IF NOT EXISTS raw_vacancies (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description_raw TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);
CREATE TABLE IF NOT EXISTS run_vacancies (
    run_id TEXT NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    PRIMARY KEY (run_id, source, external_id),
    FOREIGN KEY (source, external_id)
        REFERENCES raw_vacancies(source, external_id) ON DELETE CASCADE
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        if "source" not in self._run_columns():
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                if "source" not in self._run_columns():
                    self.db.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'hh'")

    def close(self) -> None:
        self.db.close()

    def _run_columns(self) -> set[str]:
        return {row["name"] for row in self.db.execute("PRAGMA table_info(runs)")}

    def start(self, run_id: str, source: str, query: SearchQuery, window: Window) -> None:
        with self.db:
            self.db.execute("""
                INSERT INTO runs
                    (id, source, query_key, window_from, window_to, started_at, status, tracks_cursor)
                VALUES (?, ?, ?, ?, ?, ?, 'running', 0)
            """, (run_id, source, query.key, window.start.isoformat(), window.end.isoformat(), now()))

    def finish(self, run_id: str, status: str, outcome: Collection) -> None:
        with self.db:
            self.db.execute("""
                UPDATE runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?
            """, (now(), status, json.dumps(asdict(outcome), ensure_ascii=False), run_id))

    def save(self, run_id: str, source: str, vacancy: Vacancy) -> None:
        with self.db:
            self.db.execute("""
                INSERT INTO raw_vacancies
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, external_id) DO UPDATE SET
                    url = excluded.url, title = excluded.title,
                    description_raw = excluded.description_raw,
                    published_at = excluded.published_at, fetched_at = excluded.fetched_at,
                    metadata = excluded.metadata
            """, (source, vacancy.external_id, vacancy.url, vacancy.title,
                  vacancy.description_raw, vacancy.published_at, now(),
                  json.dumps(vacancy.metadata, ensure_ascii=False)))
            self.db.execute("""
                INSERT OR IGNORE INTO run_vacancies VALUES (?, ?, ?)
            """, (run_id, source, vacancy.external_id))

    def discard_archived(self, source: str, vacancy_id: str) -> None:
        with self.db:
            self.db.execute("""
                DELETE FROM raw_vacancies WHERE source = ? AND external_id = ?
            """, (source, vacancy_id))

    def has_for_query(self, source: str, query: SearchQuery, vacancy_id: str) -> bool:
        return self.db.execute("""
            SELECT 1 FROM runs
            JOIN run_vacancies ON run_vacancies.run_id = runs.id
            WHERE runs.query_key = ? AND run_vacancies.source = ? AND external_id = ?
            LIMIT 1
        """, (query.key, source, vacancy_id)).fetchone() is not None

    def vacancies(self, limit: int | None) -> list[dict]:
        rows = self.db.execute("""
            SELECT * FROM raw_vacancies
            ORDER BY fetched_at DESC, source, external_id
            LIMIT ?
        """, (-1 if limit is None else limit,))
        result = []
        for row in rows:
            record = dict(row)
            record["metadata"] = json.loads(record["metadata"])
            result.append(record)
        return result

    def runs(self, limit: int) -> list[dict]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        results = []
        for row in rows:
            record = dict(row)
            record["summary"] = json.loads(record["summary"]) if record["summary"] else None
            results.append(record)
        return results
