from nicegui import ui


class StagePanel:
    def __init__(self, state):
        self.state = state
        with ui.card().classes("w-full"):
            self.stage = ui.label("").classes("text-lg font-bold")
            self.agent = ui.label("").classes("font-mono")
            self.at = ui.label("").classes("font-mono")
            self.updated = ui.label("").classes("text-xs text-gray-500")
            self.event = ui.label("").classes("text-sm")
        self.render()

    def render(self):
        live = self.state.live or {}
        self.stage.set_text(
            f"stage: {live.get('stage', '—')}   "
            f"status: {live.get('status', '—')}")
        self.agent.set_text(
            f"agent: {live.get('agent', '—')}   model: {live.get('model', '—')}")
        self.at.set_text(
            f"attempt={live.get('attempt', '—')} "
            f"revision={live.get('revision', '—')} "
            f"proof={live.get('proof', '—')}")
        self.updated.set_text(f"updated: {live.get('iso', '—')}")
        self.event.set_text(live.get("message", "—"))