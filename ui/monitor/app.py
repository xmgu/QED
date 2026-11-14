import os
from urllib.parse import quote

from nicegui import ui
import nicegui.run as nrun
from .state import MonitorState
from .components.header import HeaderCard
from .components.stage_panel import StagePanel
from .components.attempt_tree import AttemptTree


# NiceGUI 的页面函数只能从 URL 取参，命令行参数必须经由模块级状态传进去。
_DEFAULTS = {"dir": "", "interval": 1.5, "stale": 60.0}


def configure(args) -> None:
    """把命令行参数登记为页面默认值。"""
    _DEFAULTS["dir"] = getattr(args, "dir", "") or ""
    _DEFAULTS["interval"] = getattr(args, "interval", 1.5)
    _DEFAULTS["stale"] = getattr(args, "stale", 60.0)


def start(args):
    configure(args)
    secret = args.storage_secret or os.environ.get("QED_MONITOR_SECRET", "qed-monitor-dev")
    ui.run(host=args.host, port=args.port,
           reload=args.reload, show=False,
           storage_secret=secret,
           title="QED Monitor")


@ui.page("/monitor")
def monitor_page(dir: str = ""):
    output_dir = dir or _DEFAULTS["dir"]
    if not output_dir:
        ui.label(
            "缺少 dir 参数。请通过 URL 提供：/monitor?dir=/path/to/output，"
            "或在启动时用 --dir 指定。"
        ).classes("text-red-500")
        return

    state = MonitorState(output_dir=output_dir,
                         interval=_DEFAULTS["interval"],
                         stale_after=_DEFAULTS["stale"])
    state.refresh()

    header = HeaderCard(state)
    stage = StagePanel(state)
    tree = AttemptTree(output_dir)

    async def refresh():
        await nrun.io_bound(state.refresh)
        header.render()
        stage.render()
        if not state.alive:
            await tree.reload()
        tree.highlight(state.live if state.alive else {})

    ui.timer(state.interval, refresh, active=True)


@ui.page("/")
def index():
    ui.label("QED Monitor").classes("text-2xl font-bold")
    ui.markdown("访问 `/monitor?dir=<output_dir>` 查看实时进度。")
    ui.markdown("示例: `/monitor?dir=/home/user/qed_output`")
    ui.link("打开监控 →",
            f"/monitor?dir={quote(_DEFAULTS['dir'])}").props("flat color=primary")
