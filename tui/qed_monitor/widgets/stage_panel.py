from textual.widgets import Static
from rich.markup import escape


class StagePanel(Static):
    """顶部状态面板。

    ``stage`` 是"在做什么"，``status`` 是"做得怎么样"，两者分开显示：
    阶段结束后 stage 仍保留阶段名，只有 status 变成 DONE。
    """

    def refresh_panel(self, live, alive, stale, pid):
        status = live.get("status") if live else None
        if not live:
            state = "WAITING" if alive else "NO LIVE"
        elif live.get("_incompatible"):
            state = "INCOMPATIBLE SCHEMA"
        elif status == "ERROR":
            state = f"ERROR: {live.get('error', '')}"
        # 进程级终态；阶段级 DONE 只表示该 agent 结束，进程仍在跑。
        elif status in ("FINISHED", "STOPPED"):
            state = "DONE" if status == "FINISHED" else "STOPPED"
        elif not alive:
            state = "DEAD"
        elif stale:
            state = "STALE"
        else:
            state = "RUNNING"

        # 动态字段可能含方括号（如 repr(KeyError('[x]'))），转义后再进 Rich markup。
        lines = [
            f"[bold]state[/]   {escape(state)}   pid={pid or '—'}",
            f"[bold]stage[/]   {escape(str(live.get('stage', '—'))) if live else '—'}"
            f"   [bold]status[/] {escape(str(status or '—'))}",
            f"[bold]agent[/]   {escape(str(live.get('agent', '—'))) if live else '—'}"
            f"   [bold]model[/] {escape(str(live.get('model', '—'))) if live else '—'}",
            f"[bold]at[/]      attempt={live.get('attempt', '—') if live else '—'}"
            f" revision={live.get('revision', '—') if live else '—'}"
            f" proof={live.get('proof', '—') if live else '—'}",
            f"[bold]update[/]  {live.get('iso', '—') if live else '—'}",
            f"[bold]event[/]   {escape(str(live.get('message', '—'))) if live else '—'}",
        ]
        super().update("\n".join(lines))
