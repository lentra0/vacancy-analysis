from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from vacancy_analysis.__main__ import data_path, main
from vacancy_analysis.models import SearchQuery, Window
from vacancy_analysis.sources.hh import HHWebClient, LoadedPage, Markup, browser_tab, load_page, vacancy_data
from vacancy_analysis.orchestrator import collect
from vacancy_analysis.progress import TerminalProgress
from vacancy_analysis.storage import Storage

FIXTURES = Path(__file__).with_name("fixtures")
SEARCH = (FIXTURES / "hh-search.html").read_text(encoding="utf-8")
CARD = (FIXTURES / "hh-vacancy.html").read_text(encoding="utf-8")
SECOND_SEARCH = SEARCH.replace("/vacancy/101", "/vacancy/102")


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
                side_effect=lambda _, url: LoadedPage(200, url, self.serve(url)),
            ))
        self.enterContext(patch("vacancy_analysis.sources.hh.time.sleep"))

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
        client = HHWebClient(lambda url: LoadedPage(200, url, self.serve(url)), delay=0)
        query = SearchQuery("DevOps", "1")
        now = datetime.now(timezone.utc)
        with closing(Storage(self.database)) as storage:
            results = [
                collect(client, storage, query, Window(now, now),
                        card_limit=1, new_only=True)
                for _ in range(3)
            ]
            self.assertEqual([result["saved"] for result in results], [1, 1, 0])
            self.assertEqual([result["skipped"] for result in results], [0, 1, 2])
        self.assertEqual([urlsplit(url).path for url in self.urls if "/vacancy/" in url], [
            "/vacancy/101", "/vacancy/102",
        ])

    def test_captcha_stops_run_and_preserves_saved_cards(self):
        def load(url):
            if "/vacancy/102" in url:
                return LoadedPage(200, url, '<body><input data-qa="captcha-input"></body>', captcha_visible=True)
            return LoadedPage(200, url, self.serve(url))

        now = datetime.now(timezone.utc)
        client = HHWebClient(load, delay=0)
        with closing(Storage(self.database)) as storage:
            result = collect(client, storage, SearchQuery("DevOps", "1"), Window(now, now))
            self.assertEqual((result["status"], result["saved"], result["blocked"]),
                             ("partial", 1, True))
            self.assertEqual(result["errors"]["run"], "HH запросил капчу")

    def test_headed_check_can_continue_once_after_confirmation(self):
        checked = []

        def load(url):
            if checked:
                return LoadedPage(200, url, self.serve(url))
            return LoadedPage(403, "https://hh.ru/account/captcha",
                              '<body><input name="captchaText"></body>', captcha_visible=True)

        def confirm(reason):
            checked.append(reason)
            return True

        client = HHWebClient(load, pages=1, delay=0, confirm_check=confirm)
        self.assertEqual(list(client.vacancy_ids(SearchQuery("DevOps"))), ["101", "101"])
        self.assertEqual(checked, ["HH запросил капчу"])

        client = HHWebClient(lambda url: LoadedPage(429, url, ""), delay=0, confirm_check=confirm)
        now = datetime.now(timezone.utc)
        with closing(Storage(self.database)) as storage:
            result = collect(client, storage, SearchQuery("DevOps"), Window(now, now))
        self.assertTrue(result["blocked"])
        self.assertEqual(len(checked), 1)

    def test_inaccessible_page_without_visible_captcha_never_prompts(self):
        now = datetime.now(timezone.utc)
        for url, status, error in (
            ("https://hh.ru/search/vacancy", 403, "HH ограничил доступ (HTTP 403): /search/vacancy"),
            ("https://hh.ru/account/captcha", 200,
             "Страница проверки HH без видимого поля ввода (HTTP 200): /account/captcha"),
        ):
            with self.subTest(url=url), closing(Storage(self.database)) as storage:
                client = HHWebClient(lambda _: LoadedPage(status, url, SEARCH), delay=0,
                                     confirm_check=lambda _: self.fail("Неожиданное приглашение пройти капчу"))
                result = collect(client, storage, SearchQuery("DevOps"), Window(now, now))
                self.assertTrue(result["blocked"])
                self.assertEqual(result["errors"]["run"], error)

    @unittest.skipUnless(os.getenv("HH_TEST_CHROMIUM") == "1", "Требуется Chromium")
    def test_browser_checks_if_captcha_input_is_visible(self):
        with browser_tab(None, headed=False) as page:
            page.route("https://hh.ru/**", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body='<input name="captchaText">' if "/account/captcha" in route.request.url else SEARCH,
            ))
            self.assertFalse(load_page(page, "https://hh.ru/search/vacancy").captcha_visible)
            self.assertTrue(load_page(page, "https://hh.ru/account/captcha").captcha_visible)

    def test_run_reuses_profile_from_login(self):
        data_dir = self.database.parent / "vacancy-analysis-data"
        profile = data_dir / "hh-profile"
        calls = []
        self.enterContext(patch("vacancy_analysis.__main__.DATA_DIRECTORY", data_dir))
        self.enterContext(patch("vacancy_analysis.__main__.LEGACY_DATA_DIRECTORY", self.database.parent / "data"))
        self.enterContext(patch("builtins.input", return_value=""))

        @contextmanager
        def fixture_tab(selected, headed):
            calls.append((selected, headed))
            if os.getenv("HH_TEST_CHROMIUM") == "1":
                with browser_tab(selected, headed=False) as page:
                    if headed:
                        page.context.add_cookies([{
                            "name": "fixture_login", "value": "yes", "domain": "hh.ru",
                            "path": "/", "expires": int(time.time()) + 120,
                        }])
                    else:
                        cookies = page.context.cookies("https://hh.ru")
                        self.assertTrue(any(cookie["name"] == "fixture_login" for cookie in cookies))

                    def respond(route):
                        if not route.request.is_navigation_request():
                            route.abort()
                            return
                        url = route.request.url
                        html = "<body>Вход</body>" if "/account/login" in url else self.serve(url)
                        route.fulfill(content_type="text/html; charset=utf-8", body=html)

                    page.route("https://hh.ru/**", respond)
                    yield page
            else:
                if headed:
                    selected.mkdir(parents=True)
                yield None

        self.enterContext(patch("vacancy_analysis.__main__.browser_tab", fixture_tab))
        if os.getenv("HH_TEST_CHROMIUM") != "1":
            self.enterContext(patch(
                "vacancy_analysis.__main__.load_page",
                side_effect=lambda _, url: LoadedPage(200, url, "" if "/account/login" in url else self.serve(url)),
            ))
        self.enterContext(patch("vacancy_analysis.sources.hh.time.sleep"))
        with redirect_stderr(StringIO()):
            self.assertEqual(main(["login"]), 0)
        code, result = self.call_cli("run", "--text", "DevOps", "--limit", "1")
        self.assertEqual((code, result["saved"]), (0, 1))
        self.assertEqual(calls, [(profile, True), (profile, False)])

    def test_data_directory_prefers_new_location_and_reuses_existing_files(self):
        data_dir = self.database.parent / "vacancy-analysis-data"
        old_dir = self.database.parent / "data"
        with patch("vacancy_analysis.__main__.DATA_DIRECTORY", data_dir), patch(
            "vacancy_analysis.__main__.LEGACY_DATA_DIRECTORY", old_dir,
        ):
            old_dir.mkdir()
            (old_dir / "hh-profile").mkdir()
            self.assertEqual(data_path("hh-profile"), old_dir / "hh-profile")
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["runs"]), 0)
            self.assertTrue((data_dir / "vacancies.sqlite3").is_file())
            (old_dir / "vacancies.sqlite3").touch()
            self.assertEqual(data_path("vacancies.sqlite3"), data_dir / "vacancies.sqlite3")

    def test_publication_date_from_visible_card(self):
        html = """<body><h1 data-qa="vacancy-title">SRE</h1>
        <div data-qa="vacancy-description"><div>Мониторинг</div></div>
        <p>Вакансия опубликована 23 ноября 2026 в Москве</p></body>"""
        card = vacancy_data(Markup(html), "101", "https://hh.ru/vacancy/101")
        self.assertEqual(card.published_at, "2026-11-23")
        self.assertEqual(card.description_raw, "<div>Мониторинг</div>")


class ProgressTest(unittest.TestCase):
    def test_updates_elapsed_time_and_clears_line(self):
        class Terminal(StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        progress = TerminalProgress(terminal)
        with patch("vacancy_analysis.progress.time.monotonic", side_effect=[10, 10, 12]), patch.object(
            progress.stopped, "wait", side_effect=[False, True],
        ):
            progress.show("Выдача HH")
            progress._refresh()
        progress.clear()
        self.assertIn("\rВыдача HH [00:00]", terminal.getvalue())
        self.assertIn("\rВыдача HH [00:02]", terminal.getvalue())
        self.assertTrue(terminal.getvalue().endswith("\r"))


if __name__ == "__main__":
    unittest.main()
