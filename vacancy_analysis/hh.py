from dataclasses import dataclass, field
from datetime import datetime
import json


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
