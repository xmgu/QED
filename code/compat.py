"""Cross-platform helpers shared by the pipeline, the verifier and the UI.

The pipeline is developed on macOS/Linux but is also run on native Windows.
Two classes of difference matter here:

1. **Text encoding.** Windows defaults to a legacy code page (cp936 on a
   Chinese install, cp1252 elsewhere), while every file this project reads
   or writes is UTF-8. Without an explicit ``encoding=`` the interpreter
   decodes UTF-8 prompt templates with the ANSI code page and raises
   ``UnicodeDecodeError``. Every file handle created by this project must
   therefore pass ``encoding="utf-8"`` explicitly.

2. **Process launching.** ``subprocess`` inherits POSIX assumptions that do
   not hold on Windows:

   * ``preexec_fn=os.setsid`` and ``os.killpg`` / ``os.getpgid`` are
     POSIX-only and simply do not exist in the Windows build of ``os``.
   * ``signal.SIGKILL`` is not defined on Windows.
   * ``CREATE_NEW_PROCESS_GROUP`` -- the Windows counterpart of
     ``os.setsid`` -- disables CTRL+C for every process in the new group,
     so it is not used; ``taskkill /T`` terminates the tree instead.
   * CLIs installed through npm are ``.cmd`` shims. ``shutil.which``
     resolves them, but ``CreateProcess`` cannot execute a bare ``.cmd``
     name -- it needs the full path (or ``shell=True``).
"""

import os
import queue
import shutil
import subprocess
import sys
import threading


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

#: Encoding used for every file this project reads or writes.
UTF8 = "utf-8"

#: Error policy for decoding files. ``replace`` keeps a malformed byte from
#: aborting a long pipeline run; the affected line is corrupted but the
#: surrounding content survives.
UTF8_ERRORS = "replace"


def read_text(path: str) -> str:
    """Read a text file, returning ``""`` when it does not exist.

    The file is decoded as UTF-8 first. Some files are not UTF-8 though:
    the pipeline log captures the raw output of the shell and of the
    model CLIs, and on a Chinese Windows install those write the locale
    code page (GBK / cp936). A strict decode would raise
    ``UnicodeDecodeError`` and take the UI down; a blind "replace" would
    turn every Chinese character into a row of question marks.

    The decoding is attempted in order -- UTF-8, then cp936, then the
    locale encoding, and finally UTF-8 with replacement as a last resort.
    The locale encoding is tried *after* cp936 on purpose: when the
    interpreter runs in UTF-8 mode (``PYTHONUTF8=1``, the default on
    recent Windows builds) ``locale.getpreferredencoding()`` reports
    "utf-8" even though the bytes on disk are GBK.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""

    candidates = [UTF8, "cp936", "gbk", _locale_encoding()]
    seen: set[str] = set()
    for encoding in candidates:
        if not encoding:
            continue
        key = encoding.lower().replace("-", "")
        if key in seen:
            continue
        seen.add(key)
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(UTF8, errors=UTF8_ERRORS)


def _locale_encoding() -> str:
    """Return the locale encoding, or ``""`` when it cannot be determined."""
    import locale

    try:
        return locale.getpreferredencoding(False)
    except Exception:
        return ""


def write_text(path: str, content: str) -> None:
    """Write *content* to *path* as UTF-8, creating parent directories."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding=UTF8) as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Process launching
# ---------------------------------------------------------------------------

#: ``True`` when running on native Windows (not WSL, not Cygwin).
IS_WINDOWS = os.name == "nt"


def resolve_cli(cli_path: str) -> str:
    """Resolve a CLI name to something ``CreateProcess`` can actually launch.

    npm installs CLIs (``claude``, ``codex``, ``gemini``) as ``.cmd`` shims
    on Windows. ``shutil.which`` finds them, but ``CreateProcess`` cannot
    execute a bare ``.cmd`` name -- it raises ``FileNotFoundError``. Passing
    the resolved absolute path works for both ``.exe`` and ``.cmd`` shims.

    Returns the original value unchanged when the CLI cannot be found, so
    the caller still produces its normal "missing tool" diagnostic.
    """
    if not cli_path:
        return cli_path
    if os.path.isabs(cli_path):
        return cli_path
    resolved = shutil.which(cli_path)
    return resolved or cli_path


def spawn_kwargs() -> dict:
    """Return the platform-specific ``Popen`` keyword arguments.

    POSIX uses ``preexec_fn=os.setsid`` to detach the child into its own
    process group so the whole tree can be signalled at once.

    Windows has no ``setsid``. ``CREATE_NEW_PROCESS_GROUP`` is the
    nearest equivalent, but it comes with a documented catch: *CTRL+C
    signals are disabled for all processes within the new process group*.
    That would make the pipeline ignore Ctrl+C entirely, so it is not
    used here. The tree is terminated with ``taskkill /T`` instead,
    which does not need a process group of its own.
    """
    if IS_WINDOWS:
        return {}
    return {"preexec_fn": os.setsid}


def run_cli(cmd: list[str], *, timeout: float | None = None,
            cwd: str | None = None, env: dict | None = None,
            input_text: str | None = None) -> subprocess.CompletedProcess:
    """Run a model CLI on the calling thread, keeping it interruptible.

    ``subprocess.run`` in the calling thread responds to Ctrl+C: the
    signal reaches the child through the console and the wait is
    interrupted.

    Running the call in an executor thread -- which is how the async
    pipeline dispatches it -- breaks that. The default thread pool uses
    *non-daemon* threads, and CPython joins them in an atexit hook, so
    the interpreter refuses to exit while a call is in flight. Pressing
    Ctrl+C then appears to do nothing: the main thread unwinds, but the
    process waits for the model CLI to finish on its own.

    Use :func:`run_cli_async` from async code; this helper is for the
    synchronous callers (the verifier).
    """
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env
    if timeout is not None:
        kwargs["timeout"] = timeout

    return subprocess.run(cmd, input=input_text, **kwargs, **decode_kwargs())


def kill_process_tree(pid: int) -> None:
    """Terminate the process tree rooted at *pid*.

    On Windows ``taskkill /T`` walks the tree and kills the children as
    well -- the equivalent of the POSIX ``killpg`` used elsewhere.
    """
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return

    import signal

    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return

    # Escalate to SIGKILL only if the group is still alive.
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


# ---------------------------------------------------------------------------
# Interruptible execution
# ---------------------------------------------------------------------------

async def run_cli_async(cmd: list[str], *, timeout: float | None = None,
                        cwd: str | None = None, env: dict | None = None,
                        input_text: str | None = None):
    """Run a CLI in a way that Ctrl+C can actually interrupt.

    The pipeline dispatches model calls from async code, which normally
    means handing the blocking call to a thread pool. The default pool
    uses non-daemon threads, and CPython joins them during interpreter
    shutdown -- so the process cannot exit while a model call is still
    running. Ctrl+C would unwind the main thread but leave the CLI
    running to completion, which looks like an unresponsive program.

    This helper runs the call on a *daemon* thread instead. When the
    awaiting coroutine is cancelled -- which is what Ctrl+C does to the
    task -- the child process tree is terminated before the thread is
    abandoned, so nothing is left running.
    """
    import asyncio

    loop = asyncio.get_event_loop()
    holder: dict = {"process": None}

    def _call():
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if cwd is not None:
            kwargs["cwd"] = cwd
        if env is not None:
            kwargs["env"] = env
        if input_text is not None:
            kwargs["input"] = input_text
        if timeout is not None:
            kwargs["timeout"] = timeout

        proc = subprocess.Popen(cmd, **kwargs, **decode_kwargs())
        holder["process"] = proc
        try:
            stdout, stderr = proc.communicate()
        except BaseException:
            # The wait was interrupted: do not leave the CLI running.
            _terminate(proc)
            raise
        return subprocess.CompletedProcess(
            cmd, proc.returncode, stdout, stderr)

    def _terminate(proc) -> None:
        try:
            if IS_WINDOWS:
                kill_process_tree(proc.pid)
            else:
                kill_process_tree(proc.pid)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    future = loop.run_in_executor(_daemon_executor(), _call)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        proc = holder.get("process")
        if proc is not None:
            _terminate(proc)
        raise


_executor = None


class _DaemonThreadPool:
    """A minimal thread pool whose workers are daemon threads.

    ``concurrent.futures.ThreadPoolExecutor`` does not offer a daemon
    switch, and a thread's daemon flag cannot be changed once it has
    started ("cannot set daemon status of active thread"). So the pool is
    implemented directly: a queue of jobs, a fixed set of worker
    threads, each created as a daemon before it is started.

    Daemon threads are not joined at interpreter shutdown, which is the
    point: a model call that is still running must not keep the process
    alive after Ctrl+C.
    """

    def __init__(self, max_workers: int = 8):
        self._queue = queue.Queue()
        self._threads: list = []
        self._lock = threading.Lock()
        self._max_workers = max_workers

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures

        future = concurrent.futures.Future()

        def _run():
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)

        with self._lock:
            if len(self._threads) < self._max_workers:
                thread = threading.Thread(
                    target=self._worker, daemon=True,
                    name=f"qed-cli-{len(self._threads)}")
                self._threads.append(thread)
                thread.start()
        self._queue.put(_run)
        return future

    def _worker(self):
        while True:
            job = self._queue.get()
            try:
                job()
            finally:
                self._queue.task_done()

    def shutdown(self, wait: bool = True, **kwargs):
        self._queue.put(None)


def _daemon_executor():
    """Return the process-wide daemon thread pool."""
    global _executor
    if _executor is None:
        _executor = _DaemonThreadPool()
    return _executor


# ---------------------------------------------------------------------------
# Subprocess text decoding
# ---------------------------------------------------------------------------

def decode_kwargs() -> dict:
    """Return ``subprocess`` keyword arguments for UTF-8 text output.

    ``text=True`` alone makes the interpreter decode with the *locale*
    encoding. On a Chinese Windows install that is cp936, which cannot
    represent the UTF-8 output of the model CLIs -- the decode raises
    inside the reader thread and ``result.stdout`` silently becomes
    ``None``. Pinning the encoding keeps the output readable.
    """
    return {"encoding": UTF8, "errors": UTF8_ERRORS}


def python_executable() -> str:
    """Return the interpreter running this process."""
    return sys.executable
