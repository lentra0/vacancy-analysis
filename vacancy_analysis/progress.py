from threading import Event, Lock, Thread
import time
from typing import TextIO


class TerminalProgress:
    def __init__(self, output: TextIO):
        self.output = output
        self.enabled = output.isatty()
        self.stopped = Event()
        self.lock = Lock()
        self.thread = None
        self.message = ""
        self.started = 0.0
        self.width = 0

    def __enter__(self):
        if self.enabled:
            self.thread = Thread(target=self._refresh, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()
        self.clear()

    def show(self, message: str) -> None:
        if self.enabled:
            with self.lock:
                self.message = message
                self.started = time.monotonic()
                self._draw()

    def clear(self) -> None:
        if self.enabled:
            with self.lock:
                self.message = ""
                if self.width:
                    self.output.write("\r" + " " * self.width + "\r")
                    self.output.flush()
                    self.width = 0

    def _draw(self) -> None:
        elapsed = int(time.monotonic() - self.started)
        line = f"{self.message} [{elapsed // 60:02d}:{elapsed % 60:02d}]"
        self.output.write("\r" + line.ljust(self.width))
        self.output.flush()
        self.width = len(line)

    def _refresh(self) -> None:
        while not self.stopped.wait(1):
            with self.lock:
                if self.message:
                    self._draw()
