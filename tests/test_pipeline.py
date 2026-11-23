from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from vacancy_analysis.hh import HHQuery, Window
from vacancy_analysis.hh_web import HHWebClient
from vacancy_analysis.orchestrator import collect
from vacancy_analysis.storage import Storage

FIXTURES = Path(__file__).with_name("fixtures")


class PipelineTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = self.enterContext(closing(Storage(Path(directory) / "test.sqlite3")))
        self.query = HHQuery("DevOps", "1")
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
                return 200, url, self.search
            status, html = cards[part.rsplit("/", 1)[-1]]
            return status, url, html

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


if __name__ == "__main__":
    unittest.main()
