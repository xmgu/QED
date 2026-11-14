from nicegui import ui

COLOR = {
    "RUNNING": "green",
    "DONE": "blue",
    "STOPPED": "orange",
    "NO LIVE": "grey",
    "WAITING": "orange",
    "STALE": "orange",
    "DEAD": "red",
    "ERROR": "red",
    "INCOMPATIBLE SCHEMA": "orange",
}


class HeaderCard:
    def __init__(self, state):
        self.state = state
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-4"):
                self.run_label = ui.label("").classes("font-mono")
                self.pid_label = ui.label("").classes("font-mono")
                self.badge = ui.badge("", color="grey")
            self.dir_label = ui.label("").classes("text-xs text-gray-500")
        self.render()

    def render(self):
        s = self.state
        self.run_label.set_text(
            f"run: {s.live.get('run_id', '—') if s.live else '—'}")
        self.pid_label.set_text(f"pid: {s.pid or '—'}")
        status = s.status
        self.badge.set_text(status)
        self.badge.props(f"color={COLOR.get(status, 'grey')}")
        self.dir_label.set_text(s.output_dir)