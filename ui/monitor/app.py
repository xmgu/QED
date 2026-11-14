import os
import secrets
import sys
from urllib.parse import quote

from nicegui import ui
import nicegui.run as nrun
from .state import MonitorState
from .components.header import HeaderCard
from .components.stage_panel import StagePanel
from .components.attempt_tree import AttemptTree


# NiceGUI page functions can only take arguments from the URL, so command-line
# arguments reach them through module-level state.
_DEFAULTS = {"dir": "", "interval": 1.5, "stale": 60.0}


def configure(args) -> None:
    """Register the command-line arguments as page defaults."""
    _DEFAULTS["dir"] = getattr(args, "dir", "") or ""
    _DEFAULTS["interval"] = getattr(args, "interval", 1.5)
    _DEFAULTS["stale"] = getattr(args, "stale", 60.0)


def start(args):
    configure(args)
    secret = args.storage_secret or os.environ.get("QED_MONITOR_SECRET")
    if not secret:
        # app.storage.user needs a secret. An ephemeral one keeps the default
        # from being a guessable constant shared by every install; the only
        # cost is that browser sessions do not survive a restart, which this
        # read-only page does not depend on.
        secret = secrets.token_urlsafe(32)
        print("warning: QED_MONITOR_SECRET is not set; using an ephemeral "
              "storage secret (browser sessions will not survive a restart)",
              file=sys.stderr)
    ui.run(host=args.host, port=args.port,
           reload=args.reload, show=False,
           storage_secret=secret,
           title="QED Monitor")


@ui.page("/monitor")
async def monitor_page(dir: str = ""):
    output_dir = dir or _DEFAULTS["dir"]
    if not output_dir:
        ui.label(
            "Missing dir. Pass it in the URL (/monitor?dir=/path/to/output) "
            "or start the server with --dir."
        ).classes("text-red-500")
        return

    state = MonitorState(output_dir=output_dir,
                         interval=_DEFAULTS["interval"],
                         stale_after=_DEFAULTS["stale"])
    state.refresh()

    header = HeaderCard(state)
    stage = StagePanel(state)
    with ui.card().classes("w-full"):
        ui.label("Attempts").classes("text-sm text-gray-500")
        tree = AttemptTree(output_dir)

    live = state.live if state.alive else {}
    # Load before the first timer tick: the timer only fires after one interval,
    # and an empty tree looks exactly like a run that produced nothing.
    await tree.reload(live)

    async def refresh():
        await nrun.io_bound(state.refresh)
        header.render()
        stage.render()
        # Rescan on every tick, not only once the process is gone: attempts
        # appear while the run is live, which is when the tree is worth reading.
        await tree.reload(state.live if state.alive else {})

    ui.timer(state.interval, refresh, active=True)


@ui.page("/")
def index():
    ui.label("QED Monitor").classes("text-2xl font-bold")
    ui.markdown("Open `/monitor?dir=<output_dir>` to watch a run live.")
    ui.markdown("Example: `/monitor?dir=/home/user/qed_output`")
    ui.link("Open monitor →",
            f"/monitor?dir={quote(_DEFAULTS['dir'])}").props("flat color=primary")
