"""TUI 数据层 — 从 qed_progress 共享层 re-export。"""
from qed_progress.pid import read_pid, pid_alive  # noqa: F401

__all__ = ["read_pid", "pid_alive"]