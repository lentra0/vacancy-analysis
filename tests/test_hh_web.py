from contextlib import closing, contextmanager, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from vacancy_analysis.__main__ import main
from vacancy_analysis.hh import HHQuery, Window
from vacancy_analysis.hh_web import HHWebClient, Markup, browser_tab, load_page, vacancy_data
from vacancy_analysis.orchestrator import collect
from vacancy_analysis.storage import Storage

FIXTURES = Path(__file__).with_name("fixtures")
SEARCH = (FIXTURES / "hh-search.html").read_text(encoding="utf-8")
CARD = (FIXTURES / "hh-vacancy.html").read_text(encoding="utf-8")
SECOND_SEARCH = SEARCH.replace("/vacancy/101", "/vacancy/102").split(
    '<a data-qa="pager-next"', 1
)[0] + "</main></body></html>"


class BrowserCollectionTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(directory) / "test.sqlite3"
        self.urls = []

    def serve(self, url):
        self.urls.append(url)
        parsed = urlsplit(url)
        if parsed.path == "/search/vacancy":
            params = parse_qs(parsed.query)
            return SECOND_SEARCH if params.get("page") else SEARCH
        if parsed.path in {"/vacancy/101", "/vacancy/102"}:
            return CARD
        self.fail(f"Неожиданный URL: {url}")

    def call_cli(self, *args):
        with redirect_stdout(StringIO()) as output:
            code = main(["--db", str(self.database), *args])
        return code, json.loads(output.getvalue())

    def test_collect_and_export_json(self):
        if os.getenv("HH_TEST_CHROMIUM") == "1":
            @contextmanager
            def fixture_tab(*args):
                with browser_tab(*args) as page:
                    def fixture_response(route):
                        if route.request.is_navigation_request():
                            route.fulfill(content_type="text/html; charset=utf-8",
                                          body=self.serve(route.request.url))
                        else:
                            route.abort()

                    page.route("https://hh.ru/**", fixture_response)
                    yield page

            self.enterContext(patch("vacancy_analysis.__main__.browser_tab", fixture_tab))
        else:
            @contextmanager
            def fixture_tab(*_):
                yield None

            self.enterContext(patch("vacancy_analysis.__main__.browser_tab", fixture_tab))
            self.enterContext(patch(
                "vacancy_analysis.__main__.load_page",
                side_effect=lambda _, url: (200, url, self.serve(url)),
            ))
        self.enterContext(patch("vacancy_analysis.hh_web.time.sleep"))

        code, run = self.call_cli("run", "--text", "DevOps", "--area", "1", "--limit", "2")
        self.assertEqual((code, run["status"], run["saved"]), (0, "ok", 2))
        self.assertEqual([urlsplit(url).path for url in self.urls], [
            "/search/vacancy", "/vacancy/101", "/search/vacancy", "/vacancy/102",
        ])
        self.assertEqual(parse_qs(urlsplit(self.urls[0]).query), {
            "text": ["DevOps"], "area": ["1"], "order_by": ["publication_time"], "search_period": ["1"],
        })
        code, rows = self.call_cli("export")
        self.assertEqual(code, 0)
        self.assertEqual({row["external_id"] for row in rows}, {"101", "102"})
        self.assertEqual(rows[0]["title"], "DevOps-инженер")
        self.assertEqual(rows[0]["published_at"], "2026-11-23T11:10:00+03:00")
        self.assertIn("CI/CD &amp; мониторинг", rows[0]["description_raw"])
        self.assertEqual(rows[0]["metadata"]["key_skills"], [{"name": "Linux"}, {"name": "Kubernetes"}])
        self.assertEqual(rows[0]["metadata"]["area"], {"name": "Москва"})
        self.assertEqual(len(self.call_cli("export", "--limit", "1")[1]), 1)

    def test_watch_skips_saved_cards_and_keeps_limit(self):
        client = HHWebClient(lambda url: (200, url, self.serve(url)), delay=0)
        query = HHQuery("DevOps", "1")
        now = datetime.now(timezone.utc)
        with closing(Storage(self.database)) as storage:
            results = [
                collect(client, storage, query, Window(now, now),
                        tracks_cursor=False, card_limit=1, new_only=True)
                for _ in range(3)
            ]
            self.assertEqual([result["saved"] for result in results], [1, 1, 0])
            self.assertEqual([result["skipped"] for result in results], [0, 1, 2])
            self.assertIsNone(storage.last_success(query))
        self.assertEqual([urlsplit(url).path for url in self.urls if "/vacancy/" in url], [
            "/vacancy/101", "/vacancy/102",
        ])

    def test_captcha_stops_run_and_preserves_saved_cards(self):
        def load(url):
            html = '<body><input data-qa="captcha-input"></body>'
            return (200, url, html if "/vacancy/102" in url else self.serve(url))

        now = datetime.now(timezone.utc)
        client = HHWebClient(load, delay=0)
        with closing(Storage(self.database)) as storage:
            result = collect(client, storage, HHQuery("DevOps", "1"), Window(now, now))
            self.assertEqual((result["status"], result["saved"], result["blocked"]),
                             ("partial", 1, True))
            self.assertEqual(result["errors"]["run"], "HH запросил капчу")

    def test_publication_date_from_visible_card(self):
        html = """<body><h1 data-qa="vacancy-title">SRE</h1>
        <div data-qa="vacancy-description"><div>Мониторинг</div></div>
        <p>Вакансия опубликована 23 ноября 2026 в Москве</p></body>"""
        card = vacancy_data(Markup(html), "101", "https://hh.ru/vacancy/101")
        self.assertEqual(card["published_at"], "2026-11-23")
        self.assertEqual(card["description"], "<div>Мониторинг</div>")


if __name__ == "__main__":
    unittest.main()
