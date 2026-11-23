from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
SEARCH = (FIXTURES / "hh-search.html").read_text(encoding="utf-8")
CARD = (FIXTURES / "hh-vacancy.html").read_text(encoding="utf-8")


class TestPages(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/search/vacancy":
            page_number = parse_qs(url.query).get("page", ["0"])[0]
            content = SEARCH if page_number == "0" else SEARCH.replace("/vacancy/101", "/vacancy/102")
        elif url.path in {"/vacancy/101", "/vacancy/102"}:
            content = CARD
        else:
            self.send_error(404)
            return
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main(executable: Path) -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), TestPages) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                base_url = f"http://127.0.0.1:{server.server_port}"
                env = dict(os.environ, VACANCY_ANALYSIS_HH_BASE_URL=base_url)
                env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
                command = [str(executable.resolve()), "--db", str(Path(temporary) / "smoke.sqlite3")]
                run = subprocess.run(
                    [*command, "run", "--text", "DevOps", "--area", "1", "--limit", "2", "--delay", "1"],
                    env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=100,
                )
                if run.returncode:
                    raise SystemExit(f"{run.stdout}\n{run.stderr}")
                result = json.loads(run.stdout)
                if result["status"] != "ok" or result["saved"] != 2:
                    raise SystemExit(run.stdout)
                exported = subprocess.check_output(
                    [*command, "export"], env=env, encoding="utf-8", timeout=60,
                )
                rows = json.loads(exported)
                if {row["external_id"] for row in rows} != {"101", "102"}:
                    raise SystemExit(exported)
                if any(row["source"] != "hh" or not row["description_raw"] for row in rows):
                    raise SystemExit(exported)
                print("Exported 2 vacancies as JSON.")
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    sys.stderr.reconfigure(encoding="utf-8")
    main(Path(sys.argv[1]))
