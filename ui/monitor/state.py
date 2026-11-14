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
        # Only a process-level terminal (FINISHED/STOPPED) means the run is
        # over. A stage-level DONE just means one agent returned and the process
        # is still going, which the liveness check below reports as RUNNING.
        if status in ("FINISHED", "STOPPED"):
            return "DONE" if status == "FINISHED" else "STOPPED"
        if not self.alive:
            return "DEAD"
        if self.stale:
            return "STALE"
        return "RUNNING"