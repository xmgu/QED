import argparse
import os
import sys

# Allow `python -m ui.monitor` from any working directory: the data layer lives
# in the repo-level `qed_progress` package.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from .app import start as run  # noqa: E402


def main():
    p = argparse.ArgumentParser(prog="qed-monitor-web")
    p.add_argument("--dir", default="", help="默认 output_dir")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: localhost; the page lists arbitrary dirs)")
    p.add_argument("--port", type=int, default=8502)
    p.add_argument("--interval", type=float, default=1.5)
    p.add_argument("--stale", type=float, default=60.0)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--storage-secret", default=None,
                   help="storage_secret for app.storage.user (default: QED_MONITOR_SECRET env)")
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()