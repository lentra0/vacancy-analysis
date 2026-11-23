import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from functools import partial
from io import TextIOWrapper
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .hh import HHClient, HHError, HHQuery, TICK, Window
from .hh_web import LOGIN, HHWebClient, browser_tab, load_page
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сбор вакансий HH")
    parser.add_argument("--db", type=Path, default=Path("data/vacancies.sqlite3"),
                        help="путь к SQLite (по умолчанию data/vacancies.sqlite3)")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="запустить сбор один раз")
    watch = commands.add_parser("watch", help="повторять сбор по интервалу")
    for command in (run, watch):
        command.add_argument("--text", required=True, help="поисковый запрос")
        command.add_argument("--area", help="ID региона HH")
        command.add_argument("--source", choices=("web", "api"), help="источник (по умолчанию web)")
        command.add_argument("--profile", type=Path, help="каталог профиля Chromium")
        command.add_argument("--headed", action="store_true", help="показать окно Chromium")
        command.add_argument("--limit", type=positive_int, help="до N карточек (по умолчанию 25)")
        command.add_argument("--pages", type=positive_int, help="до N страниц (по умолчанию 3)")
        command.add_argument("--period", type=int, choices=(1, 3, 7, 30), help="дней в выдаче (по умолчанию 1)")
        command.add_argument("--delay", type=positive_int, help="пауза в секундах (по умолчанию 5)")
    run.add_argument("--from", dest="start", type=timestamp, help="начало окна API в ISO 8601")
    run.add_argument("--to", dest="end", type=timestamp, help="конец окна API в ISO 8601")
    watch.add_argument("--interval", required=True, type=positive_int, help="секунды после завершения запуска")
    login = commands.add_parser("login", help="войти в HH через Chromium")
    login.add_argument("--profile", type=Path, default=Path("data/hh-profile"),
                       help="каталог профиля (по умолчанию data/hh-profile)")
    runs = commands.add_parser("runs", help="показать результаты запусков")
    runs.add_argument("--limit", type=positive_int, default=10, help="число запусков (по умолчанию 10)")
    export = commands.add_parser("export", help="выгрузить вакансии в JSON")
    export.add_argument("--limit", type=positive_int, help="число вакансий (по умолчанию все)")
    args = parser.parse_args(argv)

    explicit_window = None
    if args.command == "run":
        if bool(args.start) != bool(args.end):
            parser.error("--from и --to задаются вместе")
        if args.start and args.end:
            if args.start >= args.end:
                parser.error("--to должен быть позже --from")
            end = floor_time(args.end)
            explicit_window = Window(floor_time(args.start), end if end == args.end else end + TICK)

    uses_browser = False
    if args.command in {"run", "watch"}:
        if not args.text.strip():
            parser.error("--text должен содержать поисковый запрос")
        uses_browser = (args.source or ("api" if explicit_window else "web")) == "web"
        if uses_browser and explicit_window:
            parser.error("для --from и --to нужен --source api")
        if not uses_browser and any((args.profile, args.headed, args.limit, args.pages, args.period, args.delay)):
            parser.error("--profile, --headed, --limit, --pages, --period и --delay доступны в режиме web")

    try:
        with ExitStack() as stack:
            if args.command == "login":
                page = stack.enter_context(browser_tab(args.profile, headed=True))
                load_page(page, LOGIN)
                print("Войдите в HH в окне Chromium и нажмите Enter здесь.", file=sys.stderr, flush=True)
                input()
                return 0
            storage = stack.enter_context(closing(Storage(args.db)))
            if args.command == "runs":
                print_json(storage.runs(args.limit))
                return 0
            if args.command == "export":
                print_json(storage.vacancies(args.limit))
                return 0
            query = HHQuery(args.text.strip(), args.area)
            if uses_browser:
                page = stack.enter_context(browser_tab(args.profile, args.headed))
                client = HHWebClient(
                    partial(load_page, page), pages=args.pages or 3,
                    period=args.period or 1, delay=args.delay or 5,
                )
            else:
                client = HHClient(
                    os.getenv("HH_TOKEN", ""),
                    os.getenv("HH_USER_AGENT", "VacancyAnalysisPOC/0.1"),
                )
            while True:
                now = datetime.now(timezone.utc)
                if uses_browser:
                    window = Window(now - timedelta(days=client.period), now)
                else:
                    window = explicit_window or next_window(storage, query, now)
                result = collect(
                    client, storage, query, window,
                    tracks_cursor=not uses_browser and explicit_window is None,
                    card_limit=(args.limit or 25) if uses_browser else None,
                    new_only=uses_browser and args.command == "watch",
                )
                print_json(result)
                if args.command == "run" or result["blocked"]:
                    return {"ok": 0, "partial": 2, "failed": 1}[result["status"]]
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (EOFError, HHError, OSError, sqlite3.Error) as error:
        print(error, file=sys.stderr)
        return 1


def run_cli() -> int:
    if isinstance(sys.stdout, TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    return main()


if __name__ == "__main__":
    sys.exit(run_cli())
