from textual.widgets import Static
from rich.markup import escape


class StagePanel(Static):
    """Top status panel.

    ``stage`` is *what* is running and ``status`` is *how it is going*; they are
    displayed separately. When a stage ends the name stays put and only the
    status becomes DONE.
    """

    def refresh_panel(self, live, alive, stale, pid):
        status = live.get("status") if live else None
        if not live:
            state = "WAITING" if alive else "NO LIVE"
        elif live.get("_incompatible"):
            state = "INCOMPATIBLE SCHEMA"
        elif status == "ERROR":
            state = f"ERROR: {live.get('error', '')}"
        # Process-level terminal; a stage-level DONE only means that agent
        # returned while the process keeps running.
        elif status in ("FINISHED", "STOPPED"):
            state = "DONE" if status == "FINISHED" else "STOPPED"
        elif not alive:
            state = "DEAD"
        elif stale:
            state = "STALE"
        else:
            state = "RUNNING"

        # Dynamic fields may contain brackets (e.g. repr(KeyError('[x]')));
        # escape them before they reach Rich markup.
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
