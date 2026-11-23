from collections.abc import Iterator
from dataclasses import asdict
import sqlite3
from typing import Protocol
from uuid import uuid4

from .hh import Collection, HHBlocked, HHError, HHQuery, Window
from .storage import Storage


class Collector(Protocol):
    def vacancy_ids(self, query: HHQuery) -> Iterator[str]: ...

    def vacancy(self, vacancy_id: str) -> dict: ...


def collect(
    client: Collector,
    storage: Storage,
    query: HHQuery,
    window: Window,
    *,
    card_limit: int | None = None,
    new_only: bool = False,
) -> dict:
    run_id = uuid4().hex
    storage.start(run_id, query, window)
    outcome = Collection()
    seen = set()
    status = "failed"
    try:
        for vacancy_id in client.vacancy_ids(query):
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
                elif isinstance(error, HHBlocked):
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
