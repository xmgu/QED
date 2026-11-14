"""读取 ``<output_dir>/.qed_ui/progress_live.json``。

写入端（``code/progress_io.py``）只写 schema 2。schema 1 的旧文件仍能读：
它把阶段名和运行状态挤在 ``stage`` 一个字段里，这里升级成 ``stage`` +
``status`` 两个字段，升级后旧运行目录在新界面里不会退化成
``INCOMPATIBLE SCHEMA``。
"""
import json
import os

SCHEMA_VERSION = 2
_LEGACY_VERSIONS = (1,)


def read_live(output_dir: str):
    path = os.path.join(output_dir, ".qed_ui", "progress_live.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        version = data.get("schema_version")
        if version == SCHEMA_VERSION:
            return data
        if version in _LEGACY_VERSIONS:
            return _upgrade(data)
        return {"_incompatible": True, **data}
    except Exception:
        return None


def _upgrade(old: dict) -> dict:
    """把 schema 1 的记录升级成 schema 2 的字段布局。

    schema 1 里 ``stage`` 同时承担两种含义：阶段名（``PROVING``），以及
    阶段/进程结束标记（``DONE``/``ERROR``）。``agent == "pipeline"`` 的那条
    记录代表整个进程，升级为 ``FINISHED``。
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
