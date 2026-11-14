# QED NiceGUI Monitor

A read-only live view of a running proof pipeline, served as a separate
[b]NiceGUI[/b] process so it works with or without Streamlit.

## How it works

It shares the `qed_progress/` data layer with the TUI and polls
`<output_dir>/.qed_ui/progress_live.json`. The attempt tree is rebuilt from
`<output_dir>/decomposition` on every refresh, so new attempts, revisions and
proofs appear while the run is still going.

## Running it

```bash
# from the repository root
pip install -r ui/monitor/requirements.txt

# default bind: 127.0.0.1:8502
python -m ui.monitor --port 8502

# then open
http://127.0.0.1:8502/monitor?dir=/path/to/proof/output
```

## Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--dir` | `""` | Default output_dir; when empty, pass `?dir=` in the URL |
| `--host` | `127.0.0.1` | Bind address. The page can read any directory it is pointed at, so it is localhost-only unless you say otherwise |
| `--port` | `8502` | Port |
| `--interval` | `1.5` | Poll interval in seconds |
| `--stale` | `60.0` | Seconds without a heartbeat before the run is shown as STALE |
| `--reload` | `false` | Hot reload (development) |
| `--storage-secret` | `QED_MONITOR_SECRET` env | Secret for `app.storage.user`; an ephemeral one is generated when neither is set |

## Relationship to Streamlit

| Stays in Streamlit | Added here |
|---|---|
| Editing `config.yaml` and `human_help` | The `/monitor` page |
| Starting / stopping / resuming the pipeline | Live stage / agent / model |
| Choosing the output directory | attempt -> revision -> proof tree |
| Run history | STALE / ERROR / DEAD states |

The two are separate processes; Streamlit links to this page with the `?dir=`
parameter already filled in.

## Architecture

```
pipeline process  --write-->  <output_dir>/.qed_ui/progress_live.json
                                      |
                            +---------v----------+
                            |  qed_progress/     |  shared read-only layer
                            |  live / pid / tree |
                            +----+----------+----+
                                 |          |
                            TUI  |          |  NiceGUI
                       (terminal) |          |  (browser, 8502)
                                 |          |
                           Streamlit (8501) -- "Open live monitor" link
```

## Optional by design

Nothing here is imported by the pipeline. Removing `ui/monitor/` and the link in
`ui/app.py` returns the project to its previous state; the run itself does not
notice.
