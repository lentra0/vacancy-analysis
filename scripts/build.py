import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    system = platform.system()
    if system not in {"Linux", "Windows"} or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise SystemExit("Сборка поддерживает Linux x64 и Windows x64.")

    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH="0")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                   check=True, cwd=ROOT, env=env)
    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconfirm",
        "--name", f"vacancy-analysis-{system.lower()}-x64",
        "--paths", str(ROOT),
        "--specpath", "build", "--workpath", "build/pyinstaller",
        "scripts/cli_entry.py",
    ], check=True, cwd=ROOT, env=env)


if __name__ == "__main__":
    main()
