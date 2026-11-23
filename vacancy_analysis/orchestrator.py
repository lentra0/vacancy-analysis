from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Protocol
from uuid import uuid4

from .hh import Collection, HHBlocked, HHError, HHQuery, TICK, Window
from .storage import Storage


class Collector(Protocol):
    def vacancy_ids(self, query: HHQuery, window: Window) -> Iterator[str]: ...

    def vacancy(self, vacancy_id: str) -> dict: ...


def floor_time(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc)
    return value.replace(minute=value.minute // 5 * 5, second=0, microsecond=0)


def next_window(storage: Storage, query: HHQuery, current_time: datetime) -> Window:
    end = floor_time(current_time)
    previous = storage.last_success(query)
    if previous:
        start = min(previous, end) - TICK
    else:
        initial = storage.first_auto_start(query) or end - timedelta(days=1)
        start = min(initial, end - TICK)
    return Window(start, end)


def collect(
    client: Collector,
    storage: Storage,
    query: HHQuery,
    window: Window,
    *,
    tracks_cursor: bool = True,
    card_limit: int | None = None,
    new_only: bool = False,
) -> dict:
    run_id = uuid4().hex
    storage.start(run_id, query, window, tracks_cursor=tracks_cursor)
    outcome = Collection()
    seen = set()
    status = "failed"
    try:
        for vacancy_id in client.vacancy_ids(query, window):
            if vacancy_id in seen:
                continue
            seen.add(vacancy_id)
            if new_only and storage.has_for_query(query, vacancy_id):
                outcome.skipped += 1
                continue
            outcome.found += 1
            try:
                vacancy = client.vacancy(vacancy_id)
            except HHError as error:
                if error.status == 404:
                    outcome.unavailable += 1
                elif isinstance(error, HHBlocked) or error.status in {400, 401, 403, 429}:
                    raise
                else:
                    outcome.errors[vacancy_id] = str(error)
            else:
                if vacancy.get("archived") is True:
                    storage.discard_archived(vacancy_id)
                    outcome.archived += 1
                else:
                    storage.save(run_id, vacancy)
                    outcome.saved += 1
            if card_limit is not None and outcome.found == card_limit:
                break

        if outcome.errors:
            status = "partial" if outcome.saved else "failed"
        else:
            status = "ok"
    except (HHError, sqlite3.Error) as error:
        outcome.errors["run"] = str(error)
        outcome.blocked = isinstance(error, HHBlocked)
        status = "partial" if outcome.saved else "failed"
    except KeyboardInterrupt:
        outcome.errors["run"] = "Запуск прерван"
        status = "partial" if outcome.saved else "failed"
        raise
    finally:
        storage.finish(run_id, status, outcome)

    return {"run_id": run_id, "status": status, **asdict(outcome)}
