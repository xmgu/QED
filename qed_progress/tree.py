"""扫描 ``<output_dir>/decomposition``，产出界面用的三层状态树。

状态取值：``accepted`` / ``revise_proof`` / ``rejected`` / ``pending`` / ``unknown``。

目录层级与落盘位置的对应关系（由真实运行产物核实）：

- ``decomposition/STATUS.md`` —— 唯一一份，记录当前 attempt/revision/proof 编号，
  但**不在** attempt 或 revision 目录里；
- ``proof_N/regulator_decision.md`` —— 只有 proof 层有，格式为
  ``## Decision: REVISE_PLAN``；
- attempt / revision 目录本身不含任何状态文件。

因此 attempt 与 revision 的状态必须由子节点汇总得出，否则永远是 ``pending``。
"""
import os
import re

PATTERNS = {
    "attempt": ("attempt_", "attempt"),
    "revision": ("revision_", "rev_"),
    "proof": ("proof_",),
}

ICON = {"accepted": "✓", "rejected": "✗", "revise_proof": "⟳", "pending": "·", "unknown": "?"}

# 汇总时的偏好顺序：一次尝试只要有一份 proof 通过，它就应当显示为已通过。
_RANK = {"accepted": 3, "revise_proof": 2, "rejected": 1, "unknown": 1, "pending": 0}

_DECISION_RE = re.compile(
    r"^#{1,6}\s*Decision\s*:\s*([A-Za-z_]+)", re.IGNORECASE | re.MULTILINE
)

_DECISION_STATUS = {
    "DONE": "accepted",
    "ACCEPTED": "accepted",
    "VERIFIED": "accepted",
    "REVISE_PROOF": "revise_proof",
    "REVISE_PLAN": "rejected",
    "REWRITE": "rejected",
}


def _match(name, key):
    return any(name.startswith(p) for p in PATTERNS[key])


def _natural_key(name: str):
    """自然排序键：``proof_2`` 排在 ``proof_10`` 之前。"""
    return [int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", name)]


def _sorted_entries(path):
    try:
        return sorted(os.listdir(path), key=_natural_key)
    except Exception:
        return []


def _status(path):
    """读取单个目录自身的状态，优先 STATUS.md，降级到 regulator_decision.md。"""
    sf = os.path.join(path, "STATUS.md")
    if os.path.exists(sf):
        return _parse_status_md(sf)
    rf = os.path.join(path, "regulator_decision.md")
    if os.path.exists(rf):
        return _parse_regulator(rf)
    return "pending"


def _aggregate(statuses):
    """由子节点状态汇总父节点状态：取偏好顺序最高者。

    没有任何子节点时返回 ``pending``——目录存在但内容未产出，正是这个含义。
    """
    best = "pending"
    for status in statuses:
        if _RANK.get(status, 1) > _RANK.get(best, 0):
            best = status
    return best


def _parse_status_md(path):
    try:
        text = open(path, encoding="utf-8").read().lower()
    except Exception:
        return "unknown"
    for kw in ("accepted", "verified"):
        if kw in text:
            return "accepted"
    for kw in ("rejected", "failed"):
        if kw in text:
            return "rejected"
    return "unknown"


def _parse_regulator(path):
    """从 regulator_decision.md 提取决策。

    优先读 ``## Decision: XXX`` 这一行。旧的子串匹配只看全文是否出现
    ``DONE``，正文里写一句 "not DONE" 就会把失败判成通过；只有在缺少
    Decision 行时才回退到子串匹配。
    """
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return "unknown"
    match = _DECISION_RE.search(text)
    if match:
        return _DECISION_STATUS.get(match.group(1).upper(), "unknown")
    if "DONE" in text:
        return "accepted"
    if "REVISE_PROOF" in text:
        return "revise_proof"
    if "REVISE_PLAN" in text or "REWRITE" in text:
        return "accepted" if "accepted" in text.lower() else "rejected"
    return "unknown"


def scan_tree(output_dir):
    decomp = os.path.join(output_dir, "decomposition")
    result = []
    entries = _sorted_entries(decomp)
    if not entries:
        return result
    for a in entries:
        if not _match(a, "attempt"):
            continue
        a_path = os.path.join(decomp, a)
        if not os.path.isdir(a_path):
            continue
        revisions = []
        for r in _sorted_entries(a_path):
            if not _match(r, "revision"):
                continue
            r_path = os.path.join(a_path, r)
            if not os.path.isdir(r_path):
                continue
            proofs = []
            for p in _sorted_entries(r_path):
                if not _match(p, "proof"):
                    continue
                p_path = os.path.join(r_path, p)
                if not os.path.isdir(p_path):
                    continue
                proofs.append({"name": p, "status": _status(p_path)})
            revision_status = (_aggregate([x["status"] for x in proofs])
                               if proofs else _status(r_path))
            revisions.append({"name": r, "status": revision_status,
                              "proofs": proofs})
        attempt_status = (_aggregate([x["status"] for x in revisions])
                          if revisions else _status(a_path))
        result.append({"name": a, "status": attempt_status,
                       "revisions": revisions})
    return result
