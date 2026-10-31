"""QED Decomposition-mode Pipeline UI.

Launch with:  streamlit run ui/app.py
"""

import datetime
import os
import sys

# Ensure the ui/ directory is on the import path so sibling modules resolve.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from utils import (
    DEFAULT_OUTPUT_ROOT,
    read_file,
    write_file,
)
from config_panel import render_config_panel
from progress_monitor import render_progress, scan_progress, format_age
from process_manager import (
    start_pipeline,
    kill_pipeline,
    is_alive,
    get_resume_options,
    prepare_resume,
    install_cleanup_handlers,
)

# Ctrl+C (or closing the terminal) must stop the pipeline too: it runs
# as a detached process tree and would otherwise keep calling the model
# API after the UI is gone.
install_cleanup_handlers()


# ---------------------------------------------------------------------------
# Path helpers for human-help files (inside the output dir)
# ---------------------------------------------------------------------------

def _prove_hh_path(out_dir: str) -> str:
    return os.path.join(out_dir, "human_help",
                        "additional_prove_human_help_global.md")


def _verify_hh_path(out_dir: str) -> str:
    return os.path.join(out_dir, "human_help",
                        "additional_verify_rule_global.md")


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="QED Decomposition Pipeline",
    page_icon="Q",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "process": None,
    "output_dir": "",
    "problem_text": "",
    "global_prove_hh": "",
    "global_verify_hh": "",
    "run_active": False,
    "show_resume": False,
}
for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ---------------------------------------------------------------------------
# Auto-refresh while pipeline is running
# ---------------------------------------------------------------------------

if st.session_state.run_active:
    st_autorefresh(interval=4000, key="auto_refresh")
    if not is_alive(st.session_state.process):
        proc = st.session_state.process
        if proc is not None and hasattr(proc, "_log_fh"):
            try:
                proc._log_fh.close()
            except OSError:
                pass
        st.session_state.run_active = False
        st.session_state.process = None


# ---------------------------------------------------------------------------
# Sidebar: configuration
# ---------------------------------------------------------------------------

config = render_config_panel()


# ---------------------------------------------------------------------------
# Title + input section
# ---------------------------------------------------------------------------

st.title("QED — Decomposition-Mode Pipeline")

st.subheader("Input")

default_out = os.path.join(
    DEFAULT_OUTPUT_ROOT,
    datetime.datetime.now().strftime("run_%Y%m%d_%H%M%S"),
)
dir_col, load_col = st.columns([4, 1])
with dir_col:
    output_dir = st.text_input(
        "Output directory",
        value=st.session_state.output_dir or default_out,
        key="output_dir_input",
        disabled=st.session_state.run_active,
    )
with load_col:
    st.markdown("")
    load_clicked = st.button(
        "Load",
        disabled=st.session_state.run_active,
        use_container_width=True,
    )

st.session_state.output_dir = output_dir

if load_clicked:
    out = st.session_state.output_dir
    if out and os.path.isdir(out):
        existing_problem = os.path.join(out, "problem.tex")
        if os.path.exists(existing_problem):
            loaded = read_file(existing_problem)
            if loaded.strip():
                st.session_state.problem_text = loaded
                st.session_state.problem_input = loaded
        st.session_state.global_prove_hh = read_file(_prove_hh_path(out))
        st.session_state.prove_hh_input = st.session_state.global_prove_hh
        st.session_state.global_verify_hh = read_file(_verify_hh_path(out))
        st.session_state.verify_hh_input = st.session_state.global_verify_hh
        st.rerun()
    else:
        st.warning(
            "Directory does not exist. Enter a valid path or click Run to create it."
        )

if st.session_state.output_dir and os.path.isdir(st.session_state.output_dir):
    st.caption(f"Current output: `{st.session_state.output_dir}`")

# Problem statement
st.warning(
    "The **problem statement** and **global human help** are locked once the pipeline starts. "
    "To change them on a fresh run, specify a new output directory. "
    "Edits to the human-help files between runs are picked up by the next agent call."
)
problem_text = st.text_area(
    "Problem statement (LaTeX)",
    value=st.session_state.problem_text,
    height=200,
    key="problem_input",
    disabled=st.session_state.run_active,
)
st.session_state.problem_text = problem_text

with st.expander("Global Human Help", expanded=False):
    prove_hh = st.text_area(
        "Prove guidance (additional_prove_human_help_global.md)",
        value=st.session_state.global_prove_hh,
        height=150,
        key="prove_hh_input",
        disabled=st.session_state.run_active,
    )
    st.session_state.global_prove_hh = prove_hh

    verify_hh = st.text_area(
        "Verify rules (additional_verify_rule_global.md)",
        value=st.session_state.global_verify_hh,
        height=150,
        key="verify_hh_input",
        disabled=st.session_state.run_active,
    )
    st.session_state.global_verify_hh = verify_hh


# ---------------------------------------------------------------------------
# Control bar
# ---------------------------------------------------------------------------

st.divider()

c_run, c_stop, c_resume, c_status = st.columns([1, 1, 1, 2])

with c_run:
    run_clicked = st.button(
        "Run",
        type="primary",
        disabled=st.session_state.run_active,
        use_container_width=True,
    )

with c_stop:
    stop_clicked = st.button(
        "Stop",
        disabled=not st.session_state.run_active,
        use_container_width=True,
    )

with c_resume:
    resume_toggle = st.button(
        "Resume From...",
        disabled=st.session_state.run_active,
        use_container_width=True,
    )


def _status_summary(scan: dict) -> str:
    state = scan.get("decomposition_state", {})
    a, r, p = (state.get("current_attempt"),
               state.get("current_revision"),
               state.get("current_proof"))
    label = state.get("state_label", "")
    if a is None and r is None and p is None and not label:
        return ""
    parts = []
    if label:
        parts.append(label)
    coord = " / ".join(
        f"{name} {val}" for name, val in (("attempt", a),
                                          ("revision", r),
                                          ("proof", p))
        if val is not None
    )
    if coord:
        parts.append(coord)
    activity = scan.get("activity") or {}
    if activity.get("stale"):
        parts.append(f"no output for {format_age(activity['age_seconds'])}")
    return " — ".join(parts)


with c_status:
    if st.session_state.run_active:
        scan = scan_progress(st.session_state.output_dir)
        summary = _status_summary(scan)
        if summary:
            st.info(f"Running... ({summary})")
        else:
            st.info("Pipeline is **running**...")
    elif st.session_state.output_dir and os.path.isdir(st.session_state.output_dir):
        scan = scan_progress(st.session_state.output_dir)
        if scan["pipeline_complete"]:
            st.success("Pipeline **complete**.")
        elif scan["decomp_failed"]:
            st.error("Decomposition **failed** — retry limits exhausted.")
        elif scan["proof_succeeded"]:
            st.warning("Proof verified, summary stage pending.")
        else:
            summary = _status_summary(scan)
            if summary:
                st.warning(f"Pipeline **stopped** ({summary}).")
            else:
                st.markdown("Pipeline **idle**.")
    else:
        st.markdown("Pipeline **idle**.")


# ---------------------------------------------------------------------------
# Run handler
# ---------------------------------------------------------------------------

if run_clicked:
    if not st.session_state.problem_text.strip():
        st.error("Please enter a problem statement.")
        st.stop()

    out_dir = st.session_state.output_dir
    os.makedirs(out_dir, exist_ok=True)

    problem_file = os.path.join(out_dir, "problem.tex")
    write_file(problem_file, st.session_state.problem_text)
    write_file(_prove_hh_path(out_dir), st.session_state.global_prove_hh)
    write_file(_verify_hh_path(out_dir), st.session_state.global_verify_hh)

    try:
        proc = start_pipeline(problem_file, out_dir, config)
    except RuntimeError as exc:
        # A missing or unusable shell is the most common cause; show the
        # actionable message instead of a raw traceback.
        st.error(str(exc))
        st.stop()
    except OSError as exc:
        st.error(f"Could not start the pipeline: {exc}")
        st.stop()

    st.session_state.process = proc
    st.session_state.run_active = True
    st.session_state.show_resume = False
    st.rerun()


# ---------------------------------------------------------------------------
# Stop handler
# ---------------------------------------------------------------------------

if stop_clicked:
    kill_pipeline(st.session_state.process)
    st.session_state.run_active = False
    st.session_state.process = None
    st.session_state.show_resume = True
    st.rerun()


# ---------------------------------------------------------------------------
# Resume handler
# ---------------------------------------------------------------------------

if resume_toggle:
    st.session_state.show_resume = not st.session_state.show_resume

if st.session_state.show_resume and not st.session_state.run_active:
    out_dir = st.session_state.output_dir
    if out_dir and os.path.isdir(out_dir):
        options = get_resume_options(out_dir)
        if options:
            st.subheader("Resume From")
            labels = [o["label"] for o in options]
            choice = st.selectbox(
                "Select resume point", labels, key="resume_choice",
            )
            selected = options[labels.index(choice)]

            if st.button("Resume Pipeline", type="primary"):
                prepare_resume(out_dir, selected)

                # Re-write input files (scratch deleted them; harmless otherwise)
                problem_file = os.path.join(out_dir, "problem.tex")
                if not os.path.exists(problem_file):
                    write_file(problem_file, st.session_state.problem_text)
                write_file(_prove_hh_path(out_dir),
                           st.session_state.global_prove_hh)
                write_file(_verify_hh_path(out_dir),
                           st.session_state.global_verify_hh)

                try:
                    proc = start_pipeline(problem_file, out_dir, config)
                except RuntimeError as exc:
                    st.error(str(exc))
                    st.stop()
                except OSError as exc:
                    st.error(f"Could not start the pipeline: {exc}")
                    st.stop()

                st.session_state.process = proc
                st.session_state.run_active = True
                st.session_state.show_resume = False
                st.rerun()
        else:
            st.info("No resume points available.")
    else:
        st.info("Enter a valid output directory above to enable resume.")


# ---------------------------------------------------------------------------
# Progress section
# ---------------------------------------------------------------------------

st.divider()

out_dir = st.session_state.output_dir
if out_dir:
    render_progress(out_dir, run_active=st.session_state.run_active)
