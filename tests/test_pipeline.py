from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.client import HTTPMessage
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from vacancy_analysis.hh import HHClient, HHQuery, Window
from vacancy_analysis.orchestrator import collect, next_window
from vacancy_analysis.storage import Storage


def card(vacancy_id, name="DevOps-инженер", archived=False):
    return {
        "id": vacancy_id,
        "name": name,
        "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
        "description": "<h3>Требования</h3><ul><li>Linux</li></ul>",
        "published_at": "2026-11-23T11:00:00+0000",
        "key_skills": [{"name": "Linux"}],
        "archived": archived,
    }


def page(*ids, found=None, pages=1):
    return {
        "items": [{"id": vacancy_id} for vacancy_id in ids],
        "found": len(ids) if found is None else found,
        "pages": pages,
    }


class PipelineTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = self.enterContext(closing(Storage(Path(directory) / "test.sqlite3")))
        self.start = datetime(2026, 11, 23, 11, tzinfo=timezone.utc)
        self.window = Window(self.start, self.start + timedelta(minutes=10))
        self.query = HHQuery("DevOps инженер", "1")
        self.client = HHClient("test-token", "test-app/1.0", retries=1)
        self.sleep = self.enterContext(patch("vacancy_analysis.hh.time.sleep"))

    def collect(self, api, *, tracks_cursor=True):
        def open_url(request, *, timeout):
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(request.get_header("User-agent"), "test-app/1.0")
            parts = urlsplit(request.full_url)
            result = api(parts.path, parse_qs(parts.query))
            if isinstance(result, int):
                headers = HTTPMessage()
                if result == 429:
                    headers["Retry-After"] = "5"
                raise HTTPError(request.full_url, result, "", headers, BytesIO())
            return BytesIO(json.dumps(result).encode())

        with patch("vacancy_analysis.hh.urlopen", side_effect=open_url):
            return collect(
                self.client, self.storage, self.query, self.window, tracks_cursor=tracks_cursor,
            )

    def test_pagination_and_upsert(self):
        def api(path, params):
            if path == "/vacancies":
                self.assertEqual(params["text"], [self.query.text])
                self.assertEqual(params["area"], ["1"])
                if params.get("page") == ["1"]:
                    return page("102", found=2, pages=2)
                return page("101", found=2, pages=2)
            return card(path.rsplit("/", 1)[1])

        result = self.collect(api)
        rerun = self.collect(lambda path, params: (
            api(path, params) if path == "/vacancies" else
            card(path.rsplit("/", 1)[1], "Обновлённая карточка")
        ))
        self.assertEqual((result["status"], result["saved"]), ("ok", 2))
        self.assertEqual((rerun["status"], rerun["saved"]), ("ok", 2))
        self.assertNotEqual(result["run_id"], rerun["run_id"])
        rows = self.storage.db.execute(
            "SELECT title, description_raw, metadata FROM raw_vacancies"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Обновлённая карточка")
        self.assertEqual(rows[0]["description_raw"], card("101")["description"])
        self.assertEqual(json.loads(rows[0]["metadata"])["key_skills"], [{"name": "Linux"}])
        links = self.storage.db.execute("SELECT count(*) FROM run_vacancies").fetchone()[0]
        self.assertEqual(links, 4)

    def test_split_uses_aligned_windows_and_deduplicates_boundary(self):
        requests = []

        def api(path, params):
            if path != "/vacancies":
                requests.append(path)
                return card(path.rsplit("/", 1)[1])
            start = params["date_from"][0]
            end = params["date_to"][0]
            if start.endswith("11:00:00+0000") and end.endswith("11:10:00+0000"):
                return page(found=2001, pages=20)
            if end.endswith("11:05:00+0000"):
                return page("101", "102")
            self.assertTrue(start.endswith("11:05:00+0000"))
            return page("102", "103")

        result = self.collect(api)
        self.assertEqual((result["status"], result["found"], result["saved"]), ("ok", 3, 3))
        self.assertEqual(requests, ["/vacancies/101", "/vacancies/102", "/vacancies/103"])

    def test_saturated_minimum_window_is_not_reported_as_success(self):
        result = self.collect(lambda *_: page(found=2001, pages=20))
        self.assertEqual(result["status"], "failed")
        self.assertIn("Лимит выдачи", result["errors"]["run"])
        self.assertIsNone(self.storage.last_success(self.query))

    def test_retry_honors_retry_after(self):
        attempts = 0

        def api(path, params):
            nonlocal attempts
            if path == "/vacancies":
                return page("101")
            attempts += 1
            return 429 if attempts == 1 else card("101")

        self.assertEqual(self.collect(api)["status"], "ok")
        self.assertEqual(attempts, 2)
        self.sleep.assert_called_once_with(5)

    def test_partial_keeps_rows_and_leaves_cursor_for_retry(self):
        self.collect(lambda *_: page())
        later = self.start + timedelta(minutes=20)
        self.window = next_window(self.storage, self.query, later)
        self.assertEqual(self.window.start, self.start + timedelta(minutes=5))

        def api(path, params):
            if path == "/vacancies":
                return page("101", "102", "103")
            if path.endswith("/101"):
                return card("101")
            return 404 if path.endswith("/102") else 503

        result = self.collect(api)
        self.assertEqual((result["status"], result["saved"], result["unavailable"]),
                         ("partial", 1, 1))
        self.assertIn("103", result["errors"])
        retry = next_window(self.storage, self.query, later)
        self.assertEqual(retry, self.window)
        self.assertEqual(self.storage.runs(1)[0]["status"], "partial")

    def test_first_failed_run_anchors_the_initial_window(self):
        self.assertEqual(self.collect(lambda *_: 503)["status"], "failed")
        later = self.start + timedelta(days=2)
        self.assertEqual(next_window(self.storage, self.query, later).start, self.window.start)

    def test_manual_window_does_not_advance_periodic_cursor(self):
        self.assertEqual(self.collect(lambda *_: page(), tracks_cursor=False)["status"], "ok")
        self.assertIsNone(self.storage.last_success(self.query))

    def test_archived_detail_removes_saved_card(self):
        self.collect(lambda path, _: page("101") if path == "/vacancies" else card("101"))
        result = self.collect(lambda path, _: (
            page("101") if path == "/vacancies" else card("101", archived=True)
        ))
        self.assertEqual((result["status"], result["archived"]), ("ok", 1))
        count = self.storage.db.execute("SELECT count(*) FROM raw_vacancies").fetchone()[0]
        self.assertEqual(count, 0)

    def test_auth_failure_stops_collection(self):
        result = self.collect(lambda path, _: (
            page("101", "102") if path == "/vacancies" else 403
        ))
        self.assertEqual((result["status"], result["found"]), ("failed", 1))
        self.assertIn("HTTP 403", result["errors"]["run"])

    def test_missing_search_results_mark_run_partial(self):
        result = self.collect(lambda path, _: (
            page("101", found=2) if path == "/vacancies" else card("101")
        ))
        self.assertEqual((result["status"], result["saved"]), ("partial", 1))
        self.assertIn("1 ID из 2", result["errors"]["run"])


if __name__ == "__main__":
    unittest.main()
