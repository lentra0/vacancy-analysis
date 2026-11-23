from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from ..models import (
    AccessBlocked, CollectionError, SearchQuery, Vacancy, VacancyArchived, VacancyUnavailable,
)

SITE = "https://hh.ru"
LOGIN = f"{SITE}/account/login"
VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
TITLE_KEYS = ("serp-item__title", "vacancy-serp__vacancy-title", "serp-item-title")
NEXT_KEYS = ("pager-next", "pagination-next", "pagination-next-page")
EMPTY_KEYS = ("vacancy-serp__no-results", "vacancy-serp__results-empty")
CAPTCHA_PATHS = {"/captcha", "/account/captcha"}
CAPTCHA_INPUTS = 'input[data-qa="captcha-input"], [data-qa="captcha-input"] input, input[name="captchaText"]'


@dataclass(frozen=True)
class LoadedPage:
    status: int
    url: str
    html: str
    captcha_visible: bool = False


@dataclass
class Element:
    attrs: dict
    start: int
    parts: list[str] = field(default_factory=list)
    html: str = ""

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


class Markup(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.html = html
        self.lines = [0, *(match.end() for match in re.finditer("\n", html))]
        self.nodes = defaultdict(list)
        self.stack = []
        self.feed(html)
        self.close()

    def source_offset(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        keys = [attrs.get("data-qa"), attrs.get("itemprop")]
        if tag in {"a", "time", "title", "h2", "body"}:
            keys.append(tag)
        if tag == "script" and attrs.get("type") == "application/ld+json":
            keys.append("ld+json")
        if tag == "link" and attrs.get("rel") == "next":
            keys.append("next")
        keys = {key for key in keys if key}
        node = None
        if keys:
            node = Element(attrs, self.source_offset() + len(self.get_starttag_text()))
            for key in keys:
                self.nodes[key].append(node)
        if tag not in VOID_TAGS:
            self.stack.append((tag, node))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                end = self.source_offset()
                for _, node in self.stack[index:]:
                    if node is not None:
                        node.html = self.html[node.start:end]
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][0] in {"script", "style"}:
            return
        for _, node in self.stack:
            if node is not None:
                node.parts.append(data)

    def first(self, *keys) -> Element | None:
        return next((self.nodes[key][0] for key in keys if self.nodes[key]), None)

    def text(self, *keys) -> str:
        node = self.first(*keys)
        return node.text if node else ""


def job_posting(markup: Markup) -> dict:
    pending = []
    for node in markup.nodes["ld+json"]:
        try:
            pending.append(json.loads(node.html))
        except json.JSONDecodeError:
            continue
    for value in pending:
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            kind = value.get("@type")
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                return value
            if "@graph" in value:
                pending.append(value["@graph"])
    return {}


def publication_date(markup: Markup, job: dict) -> str | None:
    date_node = markup.first("datePosted", "vacancy-creation-time")
    if not date_node and len(markup.nodes["time"]) == 1:
        date_node = markup.nodes["time"][0]
    value = job.get("datePosted")
    if not isinstance(value, str) and date_node:
        value = date_node.attrs.get("datetime") or date_node.attrs.get("content")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            try:
                return datetime.fromisoformat(value).isoformat()
            except ValueError:
                pass

    text = markup.text("body")
    match = re.search(r"вакансия опубликована\s+(\d{1,2})\s+(\w+)\s+(\d{4})", text, re.I)
    if match and match[2].lower() in MONTHS:
        try:
            return date(int(match[3]), MONTHS.index(match[2].lower()) + 1, int(match[1])).isoformat()
        except ValueError:
            pass
    return None


def name_field(name: str) -> dict | None:
    return {"name": name} if name else None


def vacancy_data(markup: Markup, vacancy_id: str, url: str) -> Vacancy:
    archived = markup.first("vacancy-archive-message", "vacancy-archive-title") or any(
        node.text.lower() == "вакансия в архиве" for node in markup.nodes["h2"]
    )
    if archived:
        raise VacancyArchived(f"{vacancy_id}: вакансия в архиве")
    job = job_posting(markup)
    title = markup.text("vacancy-title") or job.get("title")
    description_node = markup.first("vacancy-description")
    description = description_node.html.strip() if description_node else ""
    description = description or job.get("description")
    published_at = publication_date(markup, job)
    if not all(isinstance(value, str) and value.strip() for value in (title, description, published_at)):
        raise CollectionError(f"{vacancy_id}: неполная карточка")

    employer = job.get("hiringOrganization")
    employer = employer.get("name", "") if isinstance(employer, dict) else ""
    location = job.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else None
    address = location.get("address") if isinstance(location, dict) else None
    area = address.get("addressLocality", "") if isinstance(address, dict) else ""
    skills = [node.text for key in ("skills-element", "vacancy-key-skill")
              for node in markup.nodes[key] if node.text]
    salary = markup.text("vacancy-salary")
    metadata = {
        "area": name_field(area or markup.text("vacancy-view-location", "vacancy-view-raw-address")),
        "employer": name_field(markup.text("vacancy-company-name") or employer),
        "experience": name_field(markup.text("vacancy-experience")),
        "key_skills": [{"name": skill} for skill in dict.fromkeys(skills)],
        "professional_roles": None,
        "salary": {"text": salary} if salary else None,
        "salary_range": None,
        "work_format": None,
    }
    return Vacancy(vacancy_id, url, title.strip(), description, published_at, metadata)


def site_url(base_url: str, href: str) -> str | None:
    url = urljoin(base_url, href)
    base = urlsplit(base_url)
    target = urlsplit(url)
    regional = base.scheme == "https" and base.hostname == "hh.ru" and (
        target.hostname is not None and target.hostname.endswith(".hh.ru")
        and target.netloc == target.hostname
    )
    if target.scheme == base.scheme and (target.netloc == base.netloc or regional):
        return url
    return None


def search_page_number(url: str) -> int:
    number = parse_qs(urlsplit(url).query).get("page", ["0"])[0]
    return int(number) if number.isascii() and number.isdecimal() else 0


def next_search_href(markup: Markup, current_url: str) -> str | None:
    for key in (*NEXT_KEYS, "next"):
        for node in markup.nodes[key]:
            if href := node.attrs.get("href"):
                return href
    next_number = search_page_number(current_url) + 1
    for node in markup.nodes["pager-page"]:
        href = node.attrs.get("href")
        if href and search_page_number(href) == next_number:
            return href
    return None


class HHCheckRequired(AccessBlocked):
    pass


class HHWebClient:
    source = "hh"

    def __init__(
        self,
        load: Callable[[str], LoadedPage],
        *,
        pages: int = 3,
        period: int = 1,
        delay: float = 5,
        base_url: str = SITE,
        progress: Callable[[str], None] | None = None,
        confirm_check: Callable[[str], bool] | None = None,
    ):
        self.load = load
        self.pages = pages
        self.period = period
        self.delay = delay
        self.base_url = base_url
        self.progress = progress
        self.confirm_check = confirm_check
        self.last_loaded = None
        self.cards_opened = 0

    def _read(self, url: str, stage: str) -> tuple[Markup, str]:
        if self.last_loaded is not None:
            wait = self.delay - (time.monotonic() - self.last_loaded)
            if wait > 0:
                if self.progress:
                    self.progress("Пауза перед запросом HH")
                time.sleep(wait)
        if self.progress:
            self.progress(stage)
        try:
            loaded = self.load(url)
        finally:
            self.last_loaded = time.monotonic()
        status, final_url = loaded.status, loaded.url
        path = urlsplit(final_url).path
        if not site_url(self.base_url, final_url):
            raise CollectionError(f"Неожиданное перенаправление: {final_url}")
        if status == 429:
            raise AccessBlocked(f"HH ограничил доступ (HTTP {status}): {path}")
        if loaded.captcha_visible:
            raise HHCheckRequired("HH запросил капчу")
        if path.rstrip("/") in CAPTCHA_PATHS:
            raise AccessBlocked(f"Страница проверки HH без видимого поля ввода (HTTP {status}): {path}")
        if path.startswith("/account/login"):
            raise HHCheckRequired("HH попросил войти в аккаунт")
        if status in {401, 403}:
            raise AccessBlocked(f"HH ограничил доступ (HTTP {status}): {path}")
        if status == 404:
            raise VacancyUnavailable(f"{path}: HTTP 404")
        if status >= 400:
            raise CollectionError(f"{path}: HTTP {status}")
        return Markup(loaded.html), final_url

    def _get(self, url: str, stage: str) -> tuple[Markup, str]:
        try:
            return self._read(url, stage)
        except HHCheckRequired as error:
            if self.confirm_check and self.confirm_check(str(error)):
                return self._read(url, stage)
            raise

    def vacancy_ids(self, query: SearchQuery) -> Iterator[str]:
        self.cards_opened = 0
        params = {"text": query.text, "order_by": "publication_time", "search_period": self.period}
        if query.area:
            params["area"] = query.area
        url = f"{self.base_url}/search/vacancy?{urlencode(params)}"
        visited = set()
        for page_number in range(self.pages):
            if url in visited:
                raise CollectionError("HH повторил страницу выдачи")
            visited.add(url)
            markup, final_url = self._get(url, f"Выдача HH, страница {page_number + 1}/{self.pages}")
            if urlsplit(final_url).path.rstrip("/") != "/search/vacancy":
                raise CollectionError(f"HH перенаправил с поиска на {final_url}")
            links = [node for key in TITLE_KEYS for node in markup.nodes[key]] or markup.nodes["a"]
            ids = []
            for link in links:
                href = link.attrs.get("href")
                if not href or not site_url(self.base_url, href):
                    continue
                match = re.fullmatch(r"/vacancy/([0-9]+)/?", urlsplit(href).path)
                if match:
                    ids.append(match[1])
            if not ids:
                body = markup.text("body").lower()
                confirmed_empty = markup.first(*EMPTY_KEYS) or any(
                    phrase in body for phrase in ("по вашему запросу ничего не найдено",
                                                  "не найдено ни одной вакансии",
                                                  "по заданным критериям вакансий не найдено")
                )
                if confirmed_empty:
                    return
                raise CollectionError("В выдаче HH не найдены ссылки на вакансии")
            yield from ids

            if page_number + 1 == self.pages:
                return
            href = next_search_href(markup, final_url)
            if href is None:
                return
            next_url = site_url(self.base_url, urljoin(final_url, href))
            if not next_url or urlsplit(next_url).path != "/search/vacancy":
                raise CollectionError("В выдаче HH сломана ссылка на следующую страницу")
            url = next_url

    def vacancy(self, vacancy_id: str) -> Vacancy:
        path = f"/vacancy/{vacancy_id}"
        self.cards_opened += 1
        stage = f"Карточка HH {self.cards_opened}, ID {vacancy_id}"
        markup, final_url = self._get(self.base_url + path, stage)
        if urlsplit(final_url).path.rstrip("/") != path:
            raise CollectionError(f"{vacancy_id}: HH перенаправил на другую карточку")
        return vacancy_data(markup, vacancy_id, self.base_url + path)


@contextmanager
def browser_tab(profile: Path | None, headed: bool):
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as error:
        raise CollectionError("Установите Playwright и Chromium по инструкции в docs/hh.md") from error

    try:
        env = dict(os.environ)
        if sys.platform == "linux" and getattr(sys, "frozen", False):
            # Chromium использует системные библиотеки, а PyInstaller меняет их путь.
            original_path = env.get("LD_LIBRARY_PATH_ORIG")
            if original_path is None:
                env.pop("LD_LIBRARY_PATH", None)
            else:
                env["LD_LIBRARY_PATH"] = original_path
        with sync_playwright() as playwright:
            if profile is None:
                browser = playwright.chromium.launch(channel="chromium", headless=not headed, env=env)
                context = browser.new_context(locale="ru-RU")
            else:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), channel="chromium", headless=not headed, locale="ru-RU", env=env,
                )
            closed = False

            def mark_closed(_):
                nonlocal closed
                closed = True

            context.on("close", mark_closed)
            try:
                yield context.pages[0] if context.pages else context.new_page()
            finally:
                if not closed:
                    try:
                        context.close()
                    except Error:
                        if not closed:
                            raise
    except Error as error:
        raise CollectionError(f"Chromium: {error.message}") from error


def load_page(page, url: str) -> LoadedPage:
    from playwright.sync_api import Error

    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if response is None:
            raise CollectionError(f"Браузер не получил ответ: {url}")
        captcha_visible = page.locator(CAPTCHA_INPUTS).evaluate_all("""
            fields => fields.some(field =>
                field.offsetWidth > 0 && field.offsetHeight > 0 &&
                field.checkVisibility({opacityProperty: true, visibilityProperty: true})
            )
        """)
        return LoadedPage(response.status, page.url, page.content(), captcha_visible)
    except Error as error:
        raise CollectionError(f"Не удалось открыть {url}: {error.message}") from error
