"""Subprocess management and resume cleanup for decomposition-mode runs."""

import os
import shutil
import signal
import subprocess
import sys

from utils import (
    PROJECT_ROOT,
    RUN_SH,
    ACTIVE_CONFIG_PATH,
    save_config,
    is_survey_complete,
    decomp_root,
    attempt_dir,
    revision_dir,
    proof_dir,
    list_attempt_dirs,
    list_revision_dirs,
    list_proof_dirs,
)


# ---------------------------------------------------------------------------
# Launch / kill (pipeline-agnostic, reused from archived)
# ---------------------------------------------------------------------------

def start_pipeline(
    problem_file: str,
    output_dir: str,
    config: dict,
) -> subprocess.Popen:
    """Start the pipeline as a background subprocess.

    Writes *config* to ``.config_run_active.yaml`` at project root, then
    invokes ``run.sh <problem_file> <output_dir> <config_path>``. stdout
    and stderr are redirected to ``<output_dir>/pipeline_stdout.log``.
    """
    save_config(config, ACTIVE_CONFIG_PATH)
    config_abs = os.path.abspath(ACTIVE_CONFIG_PATH)
    problem_abs = os.path.abspath(problem_file)
    output_abs = os.path.abspath(output_dir)

    os.makedirs(output_abs, exist_ok=True)
    log_path = os.path.join(output_abs, "pipeline_stdout.log")
    log_fh = open(log_path, "a")
    env = os.environ.copy()
    env.setdefault("QED_PYTHON", sys.executable)

    proc = subprocess.Popen(
        ["bash", RUN_SH, problem_abs, output_abs, config_abs],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
        cwd=PROJECT_ROOT,
        env=env,
    )
    proc._log_fh = log_fh
    return proc


def kill_pipeline(proc: subprocess.Popen | None) -> None:
    """Kill the pipeline and its entire process tree."""
    if proc is None or proc.poll() is not None:
        _close_log(proc)
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=3)
    except (ProcessLookupError, OSError):
        pass
    _close_log(proc)


def _close_log(proc: subprocess.Popen | None) -> None:
    if proc is not None and hasattr(proc, "_log_fh"):
        try:
            proc._log_fh.close()
        except OSError:
            pass


def is_alive(proc: subprocess.Popen | None) -> bool:
    if proc is None:
        return False
    return proc.poll() is None


# ---------------------------------------------------------------------------
# Resume options (decomposition mode)
# ---------------------------------------------------------------------------

def get_resume_options(output_dir: str) -> list[dict]:
    """Enumerate resume points based on existing on-disk progress.

    Each item:

        {
            "kind": "scratch"|"after_survey"|"redo_attempt"|"redo_revision"|"redo_proof",
            "attempt": int|None,
            "revision": int|None,
            "proof": int|None,
            "label": str,
        }
    """
    options: list[dict] = [
        {
            "kind": "scratch",
            "attempt": None, "revision": None, "proof": None,
            "label": "From scratch (delete everything)",
        }
    ]

    if is_survey_complete(output_dir):
        options.append({
            "kind": "after_survey",
            "attempt": None, "revision": None, "proof": None,
            "label": "After survey (start decomposition from attempt 1)",
        })

    for n in list_attempt_dirs(output_dir):
        a_path = attempt_dir(output_dir, n)
        options.append({
            "kind": "redo_attempt",
            "attempt": n, "revision": None, "proof": None,
            "label": f"Redo attempt {n}  (deletes attempt_{n}/ and later)",
        })
        for m in list_revision_dirs(a_path):
            r_path = revision_dir(output_dir, n, m)
            options.append({
                "kind": "redo_revision",
                "attempt": n, "revision": m, "proof": None,
                "label": (
                    f"Attempt {n} / Redo revision {m}  "
                    f"(deletes revision_{m}/ and later in attempt {n})"
                ),
            })
            for k in list_proof_dirs(r_path):
                options.append({
                    "kind": "redo_proof",
                    "attempt": n, "revision": m, "proof": k,
                    "label": (
                        f"Attempt {n} / Rev {m} / Redo proof {k}  "
                        f"(deletes proof_{k}/ and later)"
                    ),
                })

    return options


# ---------------------------------------------------------------------------
# Resume cleanup
# ---------------------------------------------------------------------------

def _rm(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _wipe_stage2(output_dir: str) -> None:
    """Always clear Stage 2 artifacts so the summary agent regenerates them."""
    for name in ("proof_effort_summary.md", "error_proof_effort_summary.md"):
        _rm(os.path.join(output_dir, name))
    _rm(os.path.join(output_dir, "summary_log"))
    # Top-level proof.md is only written on success; remove so the new run
    # doesn't believe the prior attempt's proof is still valid.
    _rm(os.path.join(output_dir, "proof.md"))
    # Failure marker would block retry logic.
    _rm(os.path.join(decomp_root(output_dir), "failure_analysis.md"))


def prepare_resume(output_dir: str, option: dict) -> None:
    """Clean up *output_dir* so the next ``start_pipeline`` resumes from *option*.

    The decomposition prover's own ``detect_decomposition_resume`` reads the
    filesystem to figure out where to continue, so the UI just needs to delete
    anything past the chosen resume point.
    """
    _wipe_stage2(output_dir)

    kind = option.get("kind", "scratch")

    if kind == "scratch":
        for name in (
            "decomposition",
            "related_info",
            "literature_survey_log",
            "human_help",
            "tmp",
        ):
            _rm(os.path.join(output_dir, name))
        for name in (
            "TOKEN_USAGE.md",
            "token_usage.json",
            "pipeline_stdout.log",
            "problem.tex",
            "AUTO_RUN_STATUS.md.history",
        ):
            _rm(os.path.join(output_dir, name))
        return

    if kind == "after_survey":
        _rm(decomp_root(output_dir))
        return

    if kind == "redo_attempt":
        n = option["attempt"]
        for k in list_attempt_dirs(output_dir):
            if k >= n:
                _rm(attempt_dir(output_dir, k))
        if n == 1:
            _rm(os.path.join(decomp_root(output_dir), "plan_history.md"))
        return

    if kind == "redo_revision":
        n, m = option["attempt"], option["revision"]
        # Delete every later attempt entirely.
        for k in list_attempt_dirs(output_dir):
            if k > n:
                _rm(attempt_dir(output_dir, k))
        # Inside this attempt, delete revision m and later.
        a_path = attempt_dir(output_dir, n)
        for j in list_revision_dirs(a_path):
            if j >= m:
                _rm(os.path.join(a_path, f"revision_{j}"))
        return

    if kind == "redo_proof":
        n, m, k_target = option["attempt"], option["revision"], option["proof"]
        # Delete every later attempt entirely.
        for a in list_attempt_dirs(output_dir):
            if a > n:
                _rm(attempt_dir(output_dir, a))
        # Inside this attempt, delete every later revision.
        a_path = attempt_dir(output_dir, n)
        for j in list_revision_dirs(a_path):
            if j > m:
                _rm(os.path.join(a_path, f"revision_{j}"))
        # Inside this revision, delete this proof and later.
        r_path = revision_dir(output_dir, n, m)
        for p in list_proof_dirs(r_path):
            if p >= k_target:
                _rm(os.path.join(r_path, f"proof_{p}"))
        return
