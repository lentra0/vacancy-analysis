from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import urlsplit

from vacancy_analysis.models import SearchQuery, Vacancy, Window
from vacancy_analysis.sources.hh import HHWebClient, LoadedPage
from vacancy_analysis.orchestrator import collect
from vacancy_analysis.storage import SCHEMA, Storage

FIXTURES = Path(__file__).with_name("fixtures")


class PipelineTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(directory)
        self.storage = self.enterContext(closing(Storage(self.directory / "test.sqlite3")))
        self.query = SearchQuery("DevOps", "1")
        self.window = Window(
            datetime(2026, 11, 22, tzinfo=timezone.utc),
            datetime(2026, 11, 23, tzinfo=timezone.utc),
        )
        self.search = (FIXTURES / "hh-search.html").read_text(encoding="utf-8")
        self.card = (FIXTURES / "hh-vacancy.html").read_text(encoding="utf-8")

    def run_collection(self, cards):
        def load(url):
            part = urlsplit(url).path
            if part == "/search/vacancy":
                return LoadedPage(200, url, self.search)
            status, html = cards[part.rsplit("/", 1)[-1]]
            return LoadedPage(status, url, html)

        client = HHWebClient(load, pages=1, delay=0)
        return collect(client, self.storage, self.query, self.window)

    def test_upsert_keeps_card_and_records_both_runs(self):
        initial = self.run_collection({"101": (200, self.card)})
        updated = self.card.replace("DevOps-инженер", "SRE-инженер")
        second = self.run_collection({"101": (200, updated)})

        self.assertEqual((initial["saved"], second["saved"]), (1, 1))
        rows = self.storage.vacancies(None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "SRE-инженер")
        self.assertEqual(len(self.storage.runs(10)), 2)
        self.assertEqual(self.storage.db.execute("SELECT count(*) FROM run_vacancies").fetchone()[0], 2)

    def test_archived_card_is_removed(self):
        self.run_collection({"101": (200, self.card)})
        archived = '<body><h2>Вакансия в архиве</h2></body>'
        result = self.run_collection({"101": (200, archived)})

        self.assertEqual((result["status"], result["archived"]), ("ok", 1))
        self.assertEqual(self.storage.vacancies(None), [])

    def test_partial_run_saves_valid_card_and_reports_errors(self):
        self.search = self.search.replace("</main>", """
            <a data-qa="serp-item__title" href="/vacancy/102">Закрытая</a>
            <a data-qa="serp-item__title" href="/vacancy/103">Сбой</a>
        </main>""")
        result = self.run_collection({"101": (200, self.card), "102": (404, ""), "103": (503, "")})

        self.assertEqual(result["status"], "partial")
        self.assertEqual((result["found"], result["saved"], result["unavailable"]), (3, 1, 1))
        self.assertIn("HTTP 503", result["errors"]["103"])
        self.assertEqual(len(self.storage.vacancies(None)), 1)
        self.assertEqual(self.storage.runs(1)[0]["status"], "partial")

    def test_sources_share_storage_without_collisions(self):
        class AnotherSource:
            source = "test_source"

            def vacancy_ids(self, query):
                yield "101"

            def vacancy(self, vacancy_id):
                return Vacancy(vacancy_id, "https://example.test/101", "SRE", "<p>Linux</p>",
                               "2026-11-23", {"category": "IT"})

        self.run_collection({"101": (200, self.card)})
        other = AnotherSource()
        first = collect(other, self.storage, self.query, self.window, new_only=True)
        second = collect(other, self.storage, self.query, self.window, new_only=True)
        self.assertEqual((first["saved"], second["skipped"]), (1, 1))
        rows = self.storage.vacancies(None)
        self.assertEqual({(row["source"], row["external_id"]) for row in rows},
                         {("hh", "101"), ("test_source", "101")})
        self.assertEqual(rows[0]["metadata"], {"category": "IT"})
        self.assertEqual({run["source"] for run in self.storage.runs(10)}, {"hh", "test_source"})

        self.run_collection({"101": (200, '<body><h2>Вакансия в архиве</h2></body>')})
        self.assertEqual([row["source"] for row in self.storage.vacancies(None)], ["test_source"])

    def test_existing_hh_database_keeps_run_history(self):
        path = self.directory / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(SCHEMA.replace("summary TEXT,\n    source TEXT NOT NULL", "summary TEXT"))
            db.execute("""
                INSERT INTO runs
                VALUES ('old-run', ?, 'from', 'to', 'started', 'finished', 'ok', 0, NULL)
            """, (self.query.key,))
        with closing(Storage(path)) as storage:
            run = storage.runs(1)[0]
            self.assertEqual((run["id"], run["source"]), ("old-run", "hh"))


if __name__ == "__main__":
    unittest.main()
