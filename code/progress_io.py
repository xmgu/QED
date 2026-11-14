"""QED progress reporting. Write-only: every failure is swallowed so that the
monitor can never affect the proof pipeline itself.

Schema 2 fields:

``stage``
    The stage that is (or was last) running, e.g. ``PROVING`` or ``pipeline``.
    It is not cleared when the stage ends, so the monitor always shows *what*
    is being done instead of a ``DONE`` that overwrote it.
``status``
    How that stage went: ``RUNNING`` / ``DONE`` / ``ERROR`` / ``STOPPED``.
    Process-level terminals are written by the pipeline, stage-level ones by
    :class:`stage`.

Schema 1 squeezed the stage name and the run state into ``stage``; readers
upgrade it in ``qed_progress.live``, this module only writes schema 2.

Concurrency: stages must not nest within one ``output_dir``. Heartbeats and
terminal writes are serialised by the same lock, and ``closed`` is set under
that lock before the terminal write, so a late heartbeat cannot overwrite a
terminal state.
"""
import json
import os
import tempfile
import threading
import time

_SCHEMA = 2
_LIVE = "progress_live.json"
_PID = "pid"

# Serialises writes: the heartbeat thread and the main thread both write.
_lock = threading.Lock()

# Run-level fields repeated in every record (run_id, model, ...). Each write
# replaces the whole file, so they cannot be written once and left to persist.
_context = {}


def set_context(**fields):
    """Register fields to repeat in every subsequent record."""
    _context.update(fields)


def _dir(output_dir: str) -> str:
    return os.path.join(output_dir, ".qed_ui")


def _replace_retry(tmp: str, path: str, attempts: int = 5,
                   delay: float = 0.02) -> None:
    """``os.replace`` with a few retries.

    On Windows the reader can hold the destination open for a moment, and
    ``os.replace`` then fails with ``PermissionError`` instead of waiting for
    the handle to close. Without a retry the update is silently lost, which is
    how a run's ``FINISHED`` record can disappear and the monitor falls back to
    reporting ``DEAD``.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))


def _atomic_write_text(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_retry(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _atomic_write(path: str, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False))


def _write_locked(path: str, fields: dict) -> None:
    """Caller must hold ``_lock``."""
    data = {
        "schema_version": _SCHEMA,
        "pid": os.getpid(),
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **_context,
        **fields,
    }
    _atomic_write(path, data)


def write_pid(output_dir: str) -> None:
    # Atomic: a reader that catches the file mid-write would get "" and
    # ``int("")`` would turn the run into a momentary DEAD.
    try:
        _atomic_write_text(os.path.join(_dir(output_dir), _PID),
                           str(os.getpid()))
    except Exception:
        pass


def clear_pid(output_dir: str) -> None:
    try:
        os.unlink(os.path.join(_dir(output_dir), _PID))
    except Exception:
        pass


def write_live(output_dir: str, **fields) -> None:
    try:
        path = os.path.join(_dir(output_dir), _LIVE)
        with _lock:
            _write_locked(path, fields)
    except Exception:
        pass


class Heartbeat:
    """Rewrite the current stage periodically so that the reader can tell
    "still running" from "wedged"."""

    def __init__(self, output_dir: str, fields: dict, interval: float = 15.0):
        self.output_dir = output_dir
        self.fields = dict(fields)
        self.interval = interval
        self._stop = threading.Event()
        self._closed = False
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        path = os.path.join(_dir(self.output_dir), _LIVE)
        while not self._stop.wait(self.interval):
            try:
                with _lock:
                    # The closed flag shares the lock with the terminal write:
                    # whoever gets the lock first decides, so this write either
                    # lands before the terminal one or not at all.
                    if self._closed:
                        return
                    _write_locked(path, self.fields)
            except Exception:
                pass

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        # Close the gate first, then reap the thread. Once ``closed`` is set the
        # thread can no longer write, so the join is pure cleanup: a short
        # timeout keeps a stalled disk from delaying every stage transition.
        try:
            with _lock:
                self._closed = True
        except Exception:
            pass
        if self._t.is_alive() and threading.current_thread() is not self._t:
            self._t.join(timeout=2.0)


class stage:
    """Write RUNNING on enter, DONE on normal exit, ERROR on exception. The
    stage name is left in place so the monitor keeps showing what was running."""

    def __init__(self, output_dir: str, name: str, **fields):
        self.output_dir = output_dir
        self.name = name
        self.fields = fields
        self.hb = None

    def __enter__(self):
        write_live(self.output_dir, stage=self.name, status="RUNNING",
                   **self.fields)
        self.hb = Heartbeat(self.output_dir,
                            {"stage": self.name, "status": "RUNNING",
                             **self.fields})
        self.hb.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.hb:
            self.hb.__exit__(exc_type, exc, tb)
        if exc_type is not None:
            write_live(self.output_dir, stage=self.name, status="ERROR",
                       error=repr(exc), **self.fields)
        else:
            write_live(self.output_dir, stage=self.name, status="DONE",
                       **self.fields)
        return False
