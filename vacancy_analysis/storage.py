from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .hh import Collection, HHQuery, Window

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
    summary TEXT
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

    def close(self) -> None:
        self.db.close()

    def start(self, run_id: str, query: HHQuery, window: Window) -> None:
        with self.db:
            self.db.execute("""
                INSERT INTO runs
                    (id, query_key, window_from, window_to, started_at, status, tracks_cursor)
                VALUES (?, ?, ?, ?, ?, 'running', 0)
            """, (run_id, query.key, window.start.isoformat(),
                  window.end.isoformat(), now()))

    def finish(self, run_id: str, status: str, outcome: Collection) -> None:
        with self.db:
            self.db.execute("""
                UPDATE runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?
            """, (now(), status, json.dumps(asdict(outcome), ensure_ascii=False), run_id))

    def save(self, run_id: str, vacancy: dict) -> None:
        metadata = {key: vacancy.get(key) for key in (
            "area", "employer", "experience", "key_skills",
            "professional_roles", "salary", "salary_range", "work_format",
        )}
        with self.db:
            self.db.execute("""
                INSERT INTO raw_vacancies
                VALUES ('hh', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, external_id) DO UPDATE SET
                    url = excluded.url, title = excluded.title,
                    description_raw = excluded.description_raw,
                    published_at = excluded.published_at, fetched_at = excluded.fetched_at,
                    metadata = excluded.metadata
            """, (vacancy["id"], vacancy["alternate_url"], vacancy["name"],
                  vacancy["description"], vacancy["published_at"], now(),
                  json.dumps(metadata, ensure_ascii=False)))
            self.db.execute("""
                INSERT OR IGNORE INTO run_vacancies VALUES (?, 'hh', ?)
            """, (run_id, vacancy["id"]))

    def discard_archived(self, vacancy_id: str) -> None:
        with self.db:
            self.db.execute("""
                DELETE FROM raw_vacancies WHERE source = 'hh' AND external_id = ?
            """, (vacancy_id,))

    def has_for_query(self, query: HHQuery, vacancy_id: str) -> bool:
        return self.db.execute("""
            SELECT 1 FROM runs
            JOIN run_vacancies ON run_vacancies.run_id = runs.id
            WHERE query_key = ? AND source = 'hh' AND external_id = ?
            LIMIT 1
        """, (query.key, vacancy_id)).fetchone() is not None

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
