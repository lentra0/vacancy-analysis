from dataclasses import dataclass, field
from datetime import datetime
import json


class CollectionError(RuntimeError):
    pass


class AccessBlocked(CollectionError):
    pass


class VacancyUnavailable(CollectionError):
    pass


class VacancyArchived(CollectionError):
    pass


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class SearchQuery:
    text: str
    area: str | None = None

    @property
    def key(self) -> str:
        return json.dumps([self.text, self.area], ensure_ascii=False)


@dataclass(frozen=True)
class Vacancy:
    external_id: str
    url: str
    title: str
    description_raw: str
    published_at: str
    metadata: dict = field(default_factory=dict)


@dataclass
class Collection:
    found: int = 0
    saved: int = 0
    unavailable: int = 0
    archived: int = 0
    skipped: int = 0
    blocked: bool = False
    errors: dict[str, str] = field(default_factory=dict)
