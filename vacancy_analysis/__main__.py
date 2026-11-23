import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .hh import HHClient, HHQuery, TICK, Window
from .orchestrator import collect, floor_time, next_window
from .storage import Storage


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("ожидается целое число больше нуля")
    return number


def timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("ожидается дата ISO 8601") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("укажите часовой пояс")
    return parsed.astimezone(timezone.utc)


def print_json(value: dict | list) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Сбор вакансий HH")
    parser.add_argument("--db", type=Path, default=Path("data/vacancies.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="запустить сбор один раз")
    watch = commands.add_parser("watch", help="повторять сбор по интервалу")
    for command in (run, watch):
        command.add_argument("--text", required=True)
        command.add_argument("--area")
    run.add_argument("--from", dest="start", type=timestamp)
    run.add_argument("--to", dest="end", type=timestamp)
    watch.add_argument("--interval", required=True, type=positive_int, help="секунды между запусками")
    runs = commands.add_parser("runs", help="показать результаты запусков")
    runs.add_argument("--limit", type=positive_int, default=10)
    args = parser.parse_args()

    explicit_window = None
    if args.command == "run":
        if bool(args.start) != bool(args.end):
            parser.error("--from и --to задаются вместе")
        if args.start and args.end:
            if args.start >= args.end:
                parser.error("--to должен быть позже --from")
            end = floor_time(args.end)
            explicit_window = Window(floor_time(args.start), end if end == args.end else end + TICK)

    if args.command != "runs" and not args.text.strip():
        parser.error("--text должен содержать поисковый запрос")

    try:
        with closing(Storage(args.db)) as storage:
            if args.command == "runs":
                print_json(storage.runs(args.limit))
                return 0
            query = HHQuery(args.text.strip(), args.area)
            client = HHClient(
                os.getenv("HH_TOKEN", ""),
                os.getenv("HH_USER_AGENT", "VacancyAnalysisPOC/0.1"),
            )
            while True:
                window = explicit_window or next_window(storage, query, datetime.now(timezone.utc))
                result = collect(
                    client, storage, query, window, tracks_cursor=explicit_window is None,
                )
                print_json(result)
                if args.command == "run":
                    return {"ok": 0, "partial": 2, "failed": 1}[result["status"]]
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (OSError, sqlite3.Error) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
