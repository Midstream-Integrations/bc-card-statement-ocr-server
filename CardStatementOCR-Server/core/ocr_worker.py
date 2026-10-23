"""
Run docTR in a separate process so a read that hangs can be stopped.

docTR can't be interrupted from inside the process that is running it, so receipts are read by a
worker process that keeps the model loaded. If a read takes longer than OCR_TIMEOUT_SECONDS the
worker is killed and the caller gets OCRTimeout; a fresh worker starts on the next read.
Loading the model when a worker starts is not counted against the timeout.

Reads are done one at a time (the worker has the model loaded once, and docTR already uses every
CPU core), so a receipt waits for the one ahead of it before its own timer starts.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import threading

from core.settings import OCR_TIMEOUT

log = logging.getLogger("ocr.worker")

START_TIMEOUT = 300   # seconds allowed for a new worker to load the model

_ctx = mp.get_context("spawn")   # a clean process; forking a threaded Streamlit/PyTorch process isn't safe
_lock = threading.Lock()
_worker: "_Worker | None" = None


class OCRTimeout(Exception):
    """The read took longer than OCR_TIMEOUT_SECONDS and was stopped."""


def _serve(conn) -> None:
    """Worker process: load the model, then run (function, args) jobs until the pipe closes."""
    try:
        from core import ocr

        ocr.model()
    except BaseException as e:   # noqa: BLE001 - report anything, then exit
        conn.send(("error", f"{type(e).__name__}: {e}"))
        return
    conn.send(("ready", None))
    while True:
        try:
            fn, args = conn.recv()
        except EOFError:
            return
        try:
            conn.send(("ok", fn(*args)))
        except Exception as e:   # noqa: BLE001 - the caller decides what a failed read means
            conn.send(("error", f"{type(e).__name__}: {e}"))


class _Worker:
    def __init__(self):
        self.conn, child = _ctx.Pipe()
        self.proc = _ctx.Process(target=_serve, args=(child,), name="ocr-worker", daemon=True)
        self.proc.start()
        child.close()
        if not self.conn.poll(START_TIMEOUT):
            self.kill()
            raise RuntimeError(f"OCR worker didn't load the model within {START_TIMEOUT}s")
        kind, value = self._recv()
        if kind != "ready":
            self.kill()
            raise RuntimeError(f"OCR worker couldn't load the model: {value}")

    def _recv(self):
        try:
            return self.conn.recv()
        except (EOFError, OSError):
            return "error", f"OCR worker exited (code {self.proc.exitcode})"

    def call(self, fn, args, timeout: float):
        self.conn.send((fn, args))
        if not self.conn.poll(timeout):
            raise OCRTimeout(f"OCR took longer than {timeout:g}s")
        return self._recv()

    def kill(self):
        if self.proc.is_alive():
            self.proc.kill()
        self.proc.join(5)
        self.conn.close()


def run(fn, *args, timeout: float | None = None):
    """Call fn(*args) in the OCR worker (fn must be a module-level function). Raises OCRTimeout
    if it runs longer than `timeout` (default OCR_TIMEOUT_SECONDS), RuntimeError if it fails."""
    global _worker
    timeout = OCR_TIMEOUT if timeout is None else timeout
    with _lock:
        if _worker is None or not _worker.proc.is_alive():
            _worker = _Worker()
        try:
            kind, value = _worker.call(fn, args, timeout)
        except OCRTimeout:
            log.warning("OCR read stopped after %ss; restarting the worker", timeout)
            _worker.kill()
            _worker = None
            raise
        if kind != "ok":
            if not _worker.proc.is_alive():
                _worker.kill()
                _worker = None
            raise RuntimeError(value)
        return value
