"""QED 进度埋点。只写不读，任何异常都吞掉，绝不影响证明主流程。

字段约定（schema 2）：

``stage``
    当前/最近一个阶段名，例如 ``PROVING``、``pipeline``。阶段结束后保持不变，
    因此界面上看到的永远是"正在做什么"，而不是一个被覆写的 ``DONE``。
``status``
    该阶段的运行状态：``RUNNING`` / ``DONE`` / ``ERROR`` / ``STOPPED``。
    进程级终态由 pipeline 写入，阶段级终态由 :class:`stage` 写入。

schema 1 把阶段名与运行状态挤在 ``stage`` 一个字段里，读取端由
``qed_progress.live`` 负责升级，本模块只写 schema 2。

并发约定：同一 ``output_dir`` 的阶段不得嵌套。心跳与终态写入由同一把锁
串行化，退出时先置 ``closed`` 再写终态，从而保证迟到的行为不可能覆盖终态。
"""
import json
import os
import tempfile
import threading
import time

_SCHEMA = 2
_LIVE = "progress_live.json"
_PID = "pid"

# 写入串行化：心跳线程与主线程都可能写同一个文件。
_lock = threading.Lock()


def _dir(output_dir: str) -> str:
    return os.path.join(output_dir, ".qed_ui")


def _atomic_write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_locked(path: str, fields: dict) -> None:
    """调用方必须已持有 ``_lock``。"""
    data = {
        "schema_version": _SCHEMA,
        "pid": os.getpid(),
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **fields,
    }
    _atomic_write(path, data)


def write_pid(output_dir: str) -> None:
    try:
        p = os.path.join(_dir(output_dir), _PID)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
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
    """定期重写当前阶段，让读取端能把"仍在运行"与"卡死"区分开。"""

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
                    # 关闭标记与终态写入共用一把锁：拿到锁时若已关闭就退出，
                    # 否则本次写入一定早于终态写入落盘。
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
        # 先关闸再等线程：关闸保证此后不会再有心跳写入，join 只是回收线程，
        # 避免它在解释器退出或下一个阶段开始时仍然存活。
        try:
            with _lock:
                self._closed = True
        except Exception:
            pass
        if self._t.is_alive() and threading.current_thread() is not self._t:
            self._t.join(timeout=self.interval + 5.0)


class stage:
    """进入写 RUNNING，正常退出写 DONE，异常退出写 ERROR。阶段名保持不变。"""

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
