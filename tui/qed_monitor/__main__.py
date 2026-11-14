import argparse
import os
import sys

# Allow `python -m tui.qed_monitor` from any working directory: the data layer
# lives in the repo-level `qed_progress` package.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from .app import QEDMonitor  # noqa: E402


def main():
    p = argparse.ArgumentParser(prog="qed-monitor")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--interval", type=float, default=1.5)
    p.add_argument("--stale", type=float, default=60.0)
    p.add_argument("--pid", type=int, default=None,
                   help="pid file missing, specify manually")
    p.add_argument("--no-live", action="store_true",
                   help="degraded mode: scan tree only, no live")
    args = p.parse_args()
    QEDMonitor(args.output_dir, args.interval, args.stale,
               args.pid, args.no_live).run()


if __name__ == "__main__":
    main()