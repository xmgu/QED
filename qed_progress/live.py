"""Read ``<output_dir>/.qed_ui/progress_live.json``.

The writer (``code/progress_io.py``) only emits schema 2. Files left behind by
schema 1 are still readable: it squeezed the stage name and the run state into
the single ``stage`` field, and they are upgraded here into ``stage`` +
``status`` so an old run directory does not degrade to ``INCOMPATIBLE SCHEMA``
in the new UIs.
"""
import json
import os
import time

SCHEMA_VERSION = 2
_LEGACY_VERSIONS = (1,)


def read_live(output_dir: str):
    path = os.path.join(output_dir, ".qed_ui", "progress_live.json")
    data = None
    # The writer replaces the file atomically. On Windows a reader that lands
    # inside the rename window gets a transient PermissionError, which would
    # blank the page for one poll; retry briefly before giving up.
    for attempt in range(3):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            break
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        except OSError:
            if attempt == 2:
                return None
            time.sleep(0.01)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    version = data.get("schema_version")
    if version == SCHEMA_VERSION:
        return data
    if version in _LEGACY_VERSIONS:
        return _upgrade(data)
    return {"_incompatible": True, **data}


def _upgrade(old: dict) -> dict:
    """Upgrade a schema 1 record to the schema 2 field layout.

    In schema 1 ``stage`` carried two meanings at once: the stage name
    (``PROVING``) and the stage/process end marker (``DONE`` / ``ERROR``). The
    record with ``agent == "pipeline"`` describes the whole process, so it is
    upgraded to ``FINISHED``.
    """
    data = dict(old)
    stage = data.get("stage")
    if stage in ("DONE", "ERROR", "STOPPED"):
        if stage == "DONE" and data.get("agent") == "pipeline":
            data["status"] = "FINISHED"
        else:
            data["status"] = stage
        data["stage"] = ""
    else:
        data.setdefault("status", "RUNNING")
    data["schema_version"] = SCHEMA_VERSION
    return data
