"""TUI 数据层 — 从 qed_progress 共享层 re-export。"""
from qed_progress.live import read_live  # noqa: F401

__all__ = ["read_live"]