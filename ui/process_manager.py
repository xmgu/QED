"""Subprocess management and resume cleanup for decomposition-mode runs."""

import os
import shutil
import subprocess
import sys

# The compat helpers live next to the pipeline code, not next to the UI.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "code"))

from compat import (  # noqa: E402
    IS_WINDOWS,
    find_bash,
    kill_process_tree,
    spawn_kwargs,
    to_posix_path,
)

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

    On Windows the shell entry point is invoked through a Git Bash that is
    resolved explicitly -- a bare ``"bash"`` would resolve to the WSL
    launcher in ``System32`` and fail. Every path handed to the shell uses
    forward slashes, because bash re-parses a backslash path as a word
    whose escape sequences eat the separators.
    """
    save_config(config, ACTIVE_CONFIG_PATH)
    config_abs = os.path.abspath(ACTIVE_CONFIG_PATH)
    problem_abs = os.path.abspath(problem_file)
    output_abs = os.path.abspath(output_dir)

    os.makedirs(output_abs, exist_ok=True)
    log_path = os.path.join(output_abs, "pipeline_stdout.log")
    log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
    env = os.environ.copy()
    env.setdefault("QED_PYTHON", sys.executable)

    if IS_WINDOWS:
        bash = find_bash()
        if bash is None:
            log_fh.close()
            raise RuntimeError(
                "No usable bash found on this system. Install Git for Windows "
                "(https://git-scm.com/download/win), or set the QED_BASH "
                "environment variable to the full path of bash.exe."
            )
        _verify_bash(bash, log_fh)
        argv = [
            bash,
            to_posix_path(RUN_SH),
            to_posix_path(problem_abs),
            to_posix_path(output_abs),
            to_posix_path(config_abs),
        ]
    else:
        argv = ["bash", RUN_SH, problem_abs, output_abs, config_abs]

    proc = subprocess.Popen(
        argv,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=PROJECT_ROOT,
        env=env,
        **spawn_kwargs(),
    )
    proc._log_fh = log_fh
    return proc


def _verify_bash(bash: str, log_fh) -> None:
    """Check that *bash* can actually run the project's shell entry point.

    A WSL ``bash.exe`` accepts any command line and then fails later with a
    confusing message such as ``/bin/bash: D:WorkbuddyproofqedQEDrun.sh:
    No such file or directory`` -- the Windows path has been re-parsed as a
    POSIX word whose escape sequences ate the separators. Probing the shell
    up front turns that into an actionable error at the click of *Run*.
    """
    try:
        probe = subprocess.run(
            [bash, "-c", "echo QED_BASH_OK"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log_fh.close()
        raise RuntimeError(
            f"The bash at '{bash}' could not be started: {exc}\n\n"
            "This is usually the WSL launcher in C:\\Windows\\System32, which "
            "is not a real shell for Windows paths. Install Git for Windows "
            "and set QED_BASH to its bash.exe, e.g.\n"
            "  setx QED_BASH \"C:\\Program Files\\Git\\bin\\bash.exe\""
        ) from exc

    ok = probe.returncode == 0 and "QED_BASH_OK" in (probe.stdout or "")
    if ok:
        return
    detail = (probe.stderr or probe.stdout or "").strip()[:300]
    log_fh.close()
    raise RuntimeError(
        f"The bash at '{bash}' cannot run this project's scripts "
        f"(exit code {probe.returncode}).\n"
        f"{detail}\n\n"
        "This is usually the WSL launcher in C:\\Windows\\System32, which is "
        "not a real shell for Windows paths. Install Git for Windows and set "
        "QED_BASH to its bash.exe, e.g.\n"
        "  setx QED_BASH \"C:\\Program Files\\Git\\bin\\bash.exe\""
    )


def kill_pipeline(proc: subprocess.Popen | None) -> None:
    """Kill the pipeline and its entire process tree."""
    if proc is None or proc.poll() is not None:
        _close_log(proc)
        return
    try:
        kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
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
