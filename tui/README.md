# QED TUI Monitor

Zero-deployment terminal UI for monitoring a running QED proof pipeline.

## Quick Start

```bash
pip install "textual>=0.60,<1.0"
# 在仓库根目录执行（数据层是根级包 qed_progress）
python -m tui.qed_monitor --output-dir /path/to/proof/output
```

## Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--output-dir` | (required) | Pipeline output directory containing `.qed_ui/` |
| `--interval` | 1.5 | Poll interval in seconds |
| `--stale` | 60.0 | Seconds after which progress is considered stale |
| `--pid` | none | Specify PID manually (if pid file missing) |
| `--no-live` | false | Degraded mode: scan tree only, no live progress |

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `r` | Rescan attempt tree from disk |
| `q` | Quit |

## Architecture

```
pipeline process  ──write──▶  <output_dir>/.qed_ui/progress_live.json
                              <output_dir>/.qed_ui/pid
                                       │
                                  read (1.5s poll)
                                       │
                              qed-monitor (Textual TUI)
```

## Degraded Modes

- **No live file**: TUI shows `NO LIVE`, still displays attempt/revision/proof tree
- **Dead process**: TUI detects pid gone, shows `DEAD` state
- **Stale**: >60s since last progress update → shows `STALE`
- **Incompatible schema**: older/newer progress_io.py → shows `INCOMPATIBLE SCHEMA`
- **`--no-live`**: tree-only mode when `.qed_ui/` doesn't exist