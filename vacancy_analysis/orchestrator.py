from collections.abc import Iterator
from dataclasses import asdict
import sqlite3
from typing import Protocol
from uuid import uuid4

from .models import (
    AccessBlocked, Collection, CollectionError, SearchQuery, Vacancy, VacancyArchived,
    VacancyUnavailable, Window,
)
from .storage import Storage


class Collector(Protocol):
    source: str

    def vacancy_ids(self, query: SearchQuery) -> Iterator[str]: ...

    def vacancy(self, vacancy_id: str) -> Vacancy: ...


def collect(
    client: Collector,
    storage: Storage,
    query: SearchQuery,
    window: Window,
    *,
    card_limit: int | None = None,
    new_only: bool = False,
) -> dict:
    run_id = uuid4().hex
    storage.start(run_id, client.source, query, window)
    outcome = Collection()
    seen = set()
    status = "failed"
    try:
        for vacancy_id in client.vacancy_ids(query):
            if vacancy_id in seen:
                continue
            seen.add(vacancy_id)
            if new_only and storage.has_for_query(client.source, query, vacancy_id):
                outcome.skipped += 1
                continue
            outcome.found += 1
            try:
                vacancy = client.vacancy(vacancy_id)
                if vacancy.external_id != vacancy_id:
                    raise CollectionError(f"Ожидалась карточка {vacancy_id}, получена {vacancy.external_id}")
            except AccessBlocked:
                raise
            except VacancyUnavailable:
                outcome.unavailable += 1
            except VacancyArchived:
                storage.discard_archived(client.source, vacancy_id)
                outcome.archived += 1
            except CollectionError as error:
                outcome.errors[vacancy_id] = str(error)
            else:
                storage.save(run_id, client.source, vacancy)
                outcome.saved += 1
            if card_limit is not None and outcome.found == card_limit:
                break

        if outcome.errors:
            status = "partial" if outcome.saved else "failed"
        else:
            status = "ok"
    except (CollectionError, sqlite3.Error) as error:
        outcome.errors["run"] = str(error)
        outcome.blocked = isinstance(error, AccessBlocked)
        status = "partial" if outcome.saved else "failed"
    except KeyboardInterrupt:
        outcome.errors["run"] = "Запуск прерван"
        status = "partial" if outcome.saved else "failed"
        raise
    finally:
        storage.finish(run_id, status, outcome)

    return {"run_id": run_id, "status": status, **asdict(outcome)}
