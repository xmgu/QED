import time
from textual.app import App, ComposeResult
from textual import work
from textual.reactive import reactive

from .data.live import read_live
from .data.pid import read_pid, pid_alive
from .data.tree import scan_tree
from .widgets.stage_panel import StagePanel
from .widgets.attempt_tree import AttemptTree


class QEDMonitor(App):
    CSS = """
    StagePanel { height: 8; border: round $primary; }
    AttemptTree { height: 1fr; border: round $secondary; }
    """
    BINDINGS = [
        ("r", "rescan", "Rescan"),
        ("q", "quit", "Quit"),
    ]

    live = reactive({})
    alive = reactive(False)
    stale = reactive(False)

    def __init__(self, output_dir, interval=1.5, stale_after=60.0,
                 pid_override=None, no_live=False):
        super().__init__()
        self.output_dir = output_dir
        self.interval = interval
        self.stale_after = stale_after
        self.pid_override = pid_override
        self.no_live = no_live
        self._tree_signature = None

    def compose(self) -> ComposeResult:
        yield StagePanel(id="stage")
        yield AttemptTree(id="tree")

    def on_mount(self):
        self.set_interval(self.interval, self.poll)
        self.poll()
        self.reload_tree()

    def reload_tree(self):
        data = scan_tree(self.output_dir)
        self._tree_signature = repr(data)
        self.query_one("#tree", AttemptTree).load(data)

    @work(exclusive=True, thread=True)
    def poll(self):
        live = None if self.no_live else read_live(self.output_dir)
        pid = self.pid_override or read_pid(self.output_dir)
        alive = pid_alive(pid)
        stale = False
        if live and isinstance(live.get("ts"), (int, float)):
            stale = (time.time() - live["ts"]) > self.stale_after
        tree_data = scan_tree(self.output_dir)
        self.call_from_thread(self.apply, live, alive, stale, pid,
                              tree_data, repr(tree_data))

    def apply(self, live, alive, stale, pid, tree_data, tree_signature):
        self.live = live or {}
        self.alive = alive
        self.stale = stale
        self.query_one("#stage", StagePanel).refresh_panel(
            live=self.live, alive=alive, stale=stale, pid=pid)
        tree = self.query_one("#tree", AttemptTree)
        if tree_signature != self._tree_signature:
            # Attempts appear while the run is live. Without this the tree only
            # ever showed what existed at start-up or at the last manual rescan.
            self._tree_signature = tree_signature
            tree.load(tree_data)
        tree.highlight(self.live if alive else {})

    def action_rescan(self):
        self.reload_tree()
