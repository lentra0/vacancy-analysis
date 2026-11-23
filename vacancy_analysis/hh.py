from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import http.client
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TICK = timedelta(minutes=5)
PAGE_SIZE = 100
SEARCH_LIMIT = 2000
RETRYABLE = {429, 500, 502, 503, 504}


class HHError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class HHBlocked(HHError):
    pass


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def split(self) -> tuple["Window", "Window"]:
        ticks = (self.end - self.start) // TICK
        if ticks < 2:
            raise HHError("Лимит выдачи в окне от "
                          f"{self.start.isoformat()} до {self.end.isoformat()}")
        midpoint = self.start + TICK * (ticks // 2)
        return Window(self.start, midpoint), Window(midpoint, self.end)


@dataclass(frozen=True)
class HHQuery:
    text: str
    area: str | None

    @property
    def key(self) -> str:
        return json.dumps([self.text, self.area], ensure_ascii=False)


@dataclass
class Collection:
    found: int = 0
    saved: int = 0
    unavailable: int = 0
    archived: int = 0
    skipped: int = 0
    blocked: bool = False
    errors: dict[str, str] = field(default_factory=dict)


class HHClient:
    def __init__(
        self,
        token: str,
        user_agent: str,
        *,
        base_url: str = "https://api.hh.ru",
        retries: int = 3,
        timeout: float = 15,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Accept": "application/json", "User-Agent": user_agent}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.retries = retries
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers=self.headers)
        for attempt in range(self.retries + 1):
            delay = 2 ** attempt
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                    if not isinstance(payload, dict):
                        raise HHError(f"{path}: ожидался объект JSON")
                    return payload
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After")
                error.close()
                if status not in RETRYABLE or attempt == self.retries:
                    raise HHError(f"{path}: HTTP {status}", status) from error
                if retry_after:
                    delay = max(delay, retry_delay(retry_after))
                if delay > 60:
                    raise HHError(f"{path}: HTTP {status}, Retry-After больше 60 с",
                                  status) from error
            except (URLError, OSError, http.client.HTTPException) as error:
                if attempt == self.retries:
                    raise HHError(f"{path}: {error}") from error
            except (ValueError, UnicodeError) as error:
                raise HHError(f"{path}: некорректный JSON") from error
            time.sleep(delay)

    def vacancy_ids(self, query: HHQuery, window: Window) -> Iterator[str]:
        params = {
            "text": query.text,
            "date_from": window.start.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "date_to": window.end.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "order_by": "publication_time",
            "per_page": PAGE_SIZE,
        }
        if query.area:
            params["area"] = query.area
        first = self._get("/vacancies", params)
        try:
            found, pages = first["found"], first["pages"]
            if type(found) is not int or type(pages) is not int or min(found, pages) < 0:
                raise ValueError
        except (KeyError, ValueError) as error:
            raise HHError("/vacancies: некорректная пагинация") from error

        if found > SEARCH_LIMIT:
            for part in window.split():
                yield from self.vacancy_ids(query, part)
            return

        if pages > SEARCH_LIMIT // PAGE_SIZE:
            raise HHError("/vacancies: число страниц превышает лимит выдачи")
        seen = set()
        for page in range(max(1, pages)):
            payload = first if page == 0 else self._get("/vacancies", params | {"page": page})
            try:
                ids = [item["id"] for item in payload["items"]]
            except (KeyError, TypeError) as error:
                raise HHError("/vacancies: некорректный список вакансий") from error
            for vacancy_id in ids:
                if not isinstance(vacancy_id, str) or not vacancy_id.isascii() or not vacancy_id.isdecimal():
                    raise HHError("/vacancies: некорректный ID")
                if vacancy_id not in seen:
                    seen.add(vacancy_id)
                    yield vacancy_id
        if len(seen) < found:
            raise HHError(f"/vacancies: получено {len(seen)} ID из {found} в окне "
                          f"{window.start.isoformat()} .. {window.end.isoformat()}")

    def vacancy(self, vacancy_id: str) -> dict:
        data = self._get(f"/vacancies/{vacancy_id}")
        fields = ("name", "description", "alternate_url", "published_at")
        if data.get("id") != vacancy_id or any(
            not isinstance(data.get(key), str) for key in fields
        ):
            raise HHError(f"{vacancy_id}: неполная карточка")
        return data


def retry_delay(value: str) -> float:
    try:
        return float(int(value))
    except ValueError:
        try:
            return parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
