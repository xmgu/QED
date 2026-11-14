"""Scan ``<output_dir>/decomposition`` into the three-level tree the UIs show.

Status values: ``accepted`` / ``revise_proof`` / ``rejected`` / ``pending`` /
``unknown``.

Where each status actually lives (verified against real run output):

- ``decomposition/STATUS.md`` -- one per run; it records the current attempt /
  revision / proof index but does **not** sit inside an attempt or revision
  directory;
- ``proof_N/regulator_decision.md`` -- only the proof level has one, formatted
  as ``## Decision: REVISE_PLAN``;
- attempt and revision directories hold no status file of their own.

An attempt's or revision's status therefore has to be aggregated from its
children; reading it directly would leave every one of them at ``pending``.
"""
import os
import re

PATTERNS = {
    "attempt": ("attempt_", "attempt"),
    "revision": ("revision_", "rev_"),
    "proof": ("proof_",),
}

ICON = {"accepted": "✓", "rejected": "✗", "revise_proof": "⟳",
        "pending": "·", "unknown": "?"}

# Aggregation preference: as soon as one proof of an attempt is accepted, the
# attempt should be shown as accepted.
_RANK = {"accepted": 3, "revise_proof": 2, "rejected": 1, "unknown": 1, "pending": 0}

_DECISION_RE = re.compile(
    r"^#{1,6}\s*Decision\s*:\s*([A-Za-z_]+)", re.IGNORECASE | re.MULTILINE
)

# STATUS.md keeps its state in a markdown table row: ``| State | VERIFYING |``.
_STATE_RE = re.compile(
    r"^\|\s*State\s*\|\s*([A-Za-z_]+)\s*\|", re.IGNORECASE | re.MULTILINE
)

_DECISION_STATUS = {
    "DONE": "accepted",
    "ACCEPTED": "accepted",
    "VERIFIED": "accepted",
    "REVISE_PROOF": "revise_proof",
    "REVISE_PLAN": "rejected",
    "REWRITE": "rejected",
}

_STATE_STATUS = {
    "DONE": "accepted",
    "ACCEPTED": "accepted",
    "VERIFIED": "accepted",
    "COMPLETE": "accepted",
    "REVISE_PROOF": "revise_proof",
    "REJECTED": "rejected",
    "FAILED": "rejected",
    "REVISE_PLAN": "rejected",
    "REWRITE": "rejected",
}


def _match(name, key):
    return any(name.startswith(p) for p in PATTERNS[key])


def _natural_key(name: str):
    """Sort key so that ``proof_2`` comes before ``proof_10``."""
    return [int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", name)]


def _sorted_entries(path):
    try:
        return sorted(os.listdir(path), key=_natural_key)
    except Exception:
        return []


def _status(path):
    """Read one directory's own status: STATUS.md first, then the regulator."""
    sf = os.path.join(path, "STATUS.md")
    if os.path.exists(sf):
        return _parse_status_md(sf)
    rf = os.path.join(path, "regulator_decision.md")
    if os.path.exists(rf):
        return _parse_regulator(rf)
    return "pending"


def _aggregate(statuses):
    """Roll child statuses up into the parent: keep the most advanced one.

    No children at all means ``pending`` -- the directory exists but has not
    produced anything yet, which is exactly what that should mean.
    """
    best = "pending"
    for status in statuses:
        if _RANK.get(status, 1) > _RANK.get(best, 0):
            best = status
    return best


def _parse_status_md(path):
    """Read the ``| State | ... |`` row of a STATUS.md.

    Only explicit values count. Substring matching (the previous behaviour)
    reads a sentence such as "not verified yet" as accepted, so an explicit
    field -- or an explicit ``Decision:`` line -- is the only signal used.
    """
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return "unknown"
    match = _STATE_RE.search(text)
    if match:
        return _STATE_STATUS.get(match.group(1).upper(), "unknown")
    match = _DECISION_RE.search(text)
    if match:
        return _DECISION_STATUS.get(match.group(1).upper(), "unknown")
    return "unknown"


def _parse_regulator(path):
    """Extract the decision from regulator_decision.md.

    ``## Decision: XXX`` is authoritative. Substring matching over the whole
    document used to turn any failure whose text merely mentioned ``DONE`` into
    a pass; it remains only as a fallback for files without a Decision line.
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
