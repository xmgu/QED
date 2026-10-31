"""Start the proof pipeline.

This is the cross-platform entry point. It replaces the shell script
that used to do the same job: the shell resolved its own directory,
found a Python interpreter, ran the smoke tests, copied the global
human-help files into the output directory and launched the pipeline.

Doing that in Python rather than in a shell script means the project
behaves the same on Windows, macOS and Linux, and that no path ever has
to survive a shell's own parsing -- the recurring source of "D:path"
being read as one word with the separators eaten.

Usage::

    python code/run.py <problem.tex> <output_dir> [config.yaml]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

#: The repository root -- the parent of this file's directory.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Global human-help files copied into every run's output directory.
HUMAN_HELP_DIR = os.path.join(PROJECT_ROOT, "human_help")


def _python_executable() -> str:
    """Return the interpreter to use for the child processes.

    This process *is* the right interpreter, so it is simply reused.
    The shell script had to search for one because it could not assume
    it was already running under Python.
    """
    return sys.executable


def copy_human_help(output_dir: str) -> None:
    """Copy the global human-help files into *output_dir*.

    Existing files are left alone: the user may have edited them before
    a resume, and the pipeline reads whatever is in the output
    directory.
    """
    target_dir = os.path.join(output_dir, "human_help")
    os.makedirs(target_dir, exist_ok=True)

    if not os.path.isdir(HUMAN_HELP_DIR):
        return

    for name in sorted(os.listdir(HUMAN_HELP_DIR)):
        source = os.path.join(HUMAN_HELP_DIR, name)
        if not os.path.isfile(source):
            continue
        target = os.path.join(target_dir, name)
        if os.path.exists(target):
            continue
        try:
            shutil.copyfile(source, target)
        except OSError:
            pass


def run_smoke_tests(config_path: str) -> int:
    """Run the smoke tests, returning their exit code."""
    smoke_test = os.path.join(PROJECT_ROOT, "code", "smoke_test.py")
    print("=" * 60)
    print("  Running smoke tests...")
    print("=" * 60)
    result = subprocess.run(
        [_python_executable(), smoke_test, "--config", config_path])
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("problem", nargs="?", default=None,
                        help="path to the problem file (LaTeX)")
    parser.add_argument("output", nargs="?", default=None,
                        help="output directory for the proof and the logs")
    parser.add_argument("config", nargs="?", default=None,
                        help="path to config.yaml")
    parser.add_argument("--skip-smoke-test", action="store_true",
                        help="do not run the smoke tests first")
    args = parser.parse_args()

    problem = args.problem or os.path.join(PROJECT_ROOT, "problem", "problem.tex")
    output = args.output or os.path.join(PROJECT_ROOT, "proof_output")
    config = args.config or os.path.join(PROJECT_ROOT, "config.yaml")

    problem = os.path.abspath(problem)
    output = os.path.abspath(output)
    config = os.path.abspath(config)

    if not args.skip_smoke_test:
        code = run_smoke_tests(config)
        if code != 0:
            print("Smoke tests failed; not starting the pipeline.", flush=True)
            return code
        print("")

    os.makedirs(output, exist_ok=True)
    copy_human_help(output)

    print("=" * 60)
    print("  Proof Agent Pipeline")
    print("=" * 60)
    print(f"  Problem:  {problem}")
    print(f"  Output:   {output}")
    print(f"  Config:   {config}")
    print("")

    pipeline = os.path.join(PROJECT_ROOT, "code", "pipeline.py")
    # exec would replace this process, which is what the shell script
    # did; running it as a child keeps the exit code handling uniform.
    result = subprocess.run([
        _python_executable(), pipeline,
        "--input", problem,
        "--output", output,
        "--config", config,
    ])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
