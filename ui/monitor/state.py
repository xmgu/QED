from dataclasses import dataclass
from typing import Optional

import time

from qed_progress.live import read_live
from qed_progress.pid import read_pid, pid_alive


@dataclass
class MonitorState:
    output_dir: str
    interval: float = 1.5
    stale_after: float = 60.0
    live: Optional[dict] = None
    pid: Optional[int] = None
    alive: bool = False
    stale: bool = False

    def refresh(self) -> None:
        self.live = read_live(self.output_dir)
        self.pid = read_pid(self.output_dir)
        self.alive = pid_alive(self.pid)
        if self.live and isinstance(self.live.get("ts"), (int, float)):
            self.stale = (time.time() - self.live["ts"]) > self.stale_after
        else:
            self.stale = False

    @property
    def status(self) -> str:
        if not self.live:
            return "WAITING" if self.alive else "NO LIVE"
        if self.live.get("_incompatible"):
            return "INCOMPATIBLE SCHEMA"
        status = self.live.get("status")
        if status == "ERROR":
            return "ERROR"
        # 只有进程级终态（FINISHED/STOPPED）才算结束。阶段级的 DONE 表示某个
        # agent 跑完了、进程还在继续，落到下面的存活判断即为 RUNNING。
        if status in ("FINISHED", "STOPPED"):
            return "DONE" if status == "FINISHED" else "STOPPED"
        if not self.alive:
            return "DEAD"
        if self.stale:
            return "STALE"
        return "RUNNING"