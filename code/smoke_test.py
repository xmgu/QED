#!/usr/bin/env python3
"""
Smoke test: validates prompt loading and agent connectivity
without running the full proof loop (which is expensive).
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import load_prompt, make_claude_options
from decomposition_prover import (
    load_prompt as decomp_load_prompt,
    parse_report_verdict,
    compute_verdict,
    assemble_proof_from_steps,
    carry_forward_checkpoints,
    interrupted_partial,
    step_order,
    steps_dir,
    halt_run,
    DecompositionLogger,
    HALT_EXIT_CODE,
)
from model_runner import TokenExhausted, ModelRunnerError
from compat import decode_kwargs, read_text, resolve_cli, write_text


async def run_smoke_test(config: dict, config_path: str | None = None) -> bool:
    """Run all smoke tests. Returns True if all passed, False if any failed.

    Can be called from pipeline.py at startup, or standalone via main().

    Args:
        config: Parsed config dict (from yaml.safe_load).
        config_path: Path to config.yaml (used to resolve project paths).
            If None, resolves from this file's location.
    """
    # Resolve project root: config.yaml lives at project_base/config.yaml
    if config_path:
        project_base = os.path.dirname(os.path.abspath(config_path))
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_base = os.path.dirname(script_dir)

    prompts_dir = os.path.join(project_base, "prompts")
    skill_dir = os.path.join(project_base, "skill")

    claude_cfg = config.get("claude", {})

    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = ""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} -- {detail}")
            failed += 1

    # -------------------------------------------------------
    # Test 1: Prompt files exist
    # -------------------------------------------------------
    print("\n=== Test 1: Prompt files ===")
    prompt_files = [
        "literature_survey.md",
        "proof_effort_summary.md",
    ]
    for pf in prompt_files:
        exists = os.path.exists(os.path.join(prompts_dir, pf))
        check(f"Prompt {pf} exists", exists)

    # Decomposition mode prompts
    decomposition_prompt_files = [
        "decomposition-prover/decomposition.md",
        "decomposition-prover/single_prover.md",
        "decomposition-prover/regulator.md",
        "decomposition-prover/proof_verify_structural.md",
        "decomposition-prover/proof_verify_detailed.md",
    ]
    for pf in decomposition_prompt_files:
        exists = os.path.exists(os.path.join(prompts_dir, pf))
        check(f"Prompt {pf} exists", exists)

    # -------------------------------------------------------
    # Test 2: Skill files exist
    # -------------------------------------------------------
    print("\n=== Test 2: Skill files ===")
    skill_files = ["super_math_skill.md"]
    for sf in skill_files:
        exists = os.path.exists(os.path.join(skill_dir, sf))
        check(f"Skill {sf} exists", exists)

    # -------------------------------------------------------
    # Test 3: Prompt loading with variable substitution
    # -------------------------------------------------------
    print("\n=== Test 3: Prompt loading ===")
    try:
        prompt = load_prompt(
            prompts_dir, "literature_survey.md",
            problem_file="/tmp/test_problem.tex",
            related_info_dir="/tmp/test_output/related_info",
            output_dir="/tmp/test_output",
            proof_file="/tmp/test_output/proof.md",
            error_file="/tmp/test_output/error_literature_survey.md",
        )
        check("literature_survey.md renders OK", "test_problem.tex" in prompt)
    except Exception as e:
        check("literature_survey.md renders OK", False, str(e))

    try:
        prompt = load_prompt(
            prompts_dir, "proof_effort_summary.md",
            output_dir="/tmp/test_output",
            outcome="PASS",
            total_attempts=2,
            total_proofs=3,
            max_attempts=2,
            summary_file="/tmp/test_output/proof_effort_summary.md",
            error_file="/tmp/test_output/error_proof_effort_summary.md",
        )
        check("proof_effort_summary.md renders OK", "test_output" in prompt)
    except Exception as e:
        check("proof_effort_summary.md renders OK", False, str(e))

    # -------------------------------------------------------
    # Test 3b: Decomposition mode prompt loading
    # -------------------------------------------------------
    print("\n=== Test 3b: Decomposition prompt loading ===")

    try:
        prompt = load_prompt(
            prompts_dir, "decomposition-prover/decomposition.md",
            mode="CREATE",
            problem_file="/tmp/test_problem.tex",
            related_work_file="/tmp/test_related_work.md",
            problem_id="test_problem",
            attempt_number=1,
            revision_number=1,
            timestamp="2024-01-01T00:00:00",
            output_file="/tmp/test_decomposition.yaml",
            current_decomposition_file="",
            verification_feedback="",
            regulator_guidance="",
            previous_proof_file="",
            human_help_file="/tmp/test_human_help.md",
            plan_history_file="/tmp/test_plan_history.md",
        )
        check("decomposition.md renders OK", "test_problem.tex" in prompt)
        check("decomposition.md has mode", "CREATE" in prompt)
        check("decomposition.md references plan history",
              "plan_history" in prompt or "Plan History" in prompt)
        check("decomposition.md no longer references difficulty",
              "difficulty_file" not in prompt and "Difficulty Evaluation" not in prompt)
        check("decomposition.md no longer references revision_context",
              "{revision_context}" not in prompt)
    except Exception as e:
        check("decomposition.md renders OK", False, str(e))

    try:
        prompt = load_prompt(
            prompts_dir, "decomposition-prover/single_prover.md",
            problem_file="/tmp/test_problem.tex",
            related_work_file="/tmp/test_related_work.md",
            decomposition_file="/tmp/test_decomposition.yaml",
            human_help_file="/tmp/test_human_help.md",
            previous_proof_file="",
            previous_verification_file="",
            previous_partial_file="",
            carried_steps="none",
            remaining_steps="none",
            output_file="/tmp/test_proof.md",
            output_dir="/tmp/test_output",
            scratchpad_file="/tmp/test_output/scratchpad.md",
            proof_steps_dir="/tmp/test_output/proof_steps",
        )
        check("single_prover.md renders OK", "test_problem.tex" in prompt)
        check("single_prover.md has decomposition file", "test_decomposition.yaml" in prompt)
        check("single_prover.md has continuation section",
              "interrupted, not judged" in prompt
              and "{previous_partial_file}" not in prompt,
              "the section must render, not leak the placeholder name")
    except Exception as e:
        check("single_prover.md renders OK", False, str(e))

    try:
        prompt = load_prompt(
            prompts_dir, "decomposition-prover/regulator.md",
            mode="DECIDE",
            state_file="attempt: 1\nrevision: 1\nproof: 1",
            decomposition_file="/tmp/test_decomposition.yaml",
            proof_file="/tmp/test_proof.md",
            verification_report="Test verification report",
            attempt_history="First attempt",
            max_proof_attempts=3,
            max_revisions=2,
            max_decompositions=2,
            output_file="/tmp/test_regulator_decision.md",
            plan_history_file="/tmp/test_plan_history.md",
            verification_phase="structural",
        )
        check("regulator.md renders OK", "DECIDE" in prompt)
        check("regulator.md references plan history append",
              "Plan History Append" in prompt)
        check("regulator.md surfaces verification_phase",
              "structural" in prompt and "Verification Phase" in prompt)
    except Exception as e:
        check("regulator.md renders OK", False, str(e))

    # decomposition_prover.load_prompt MUST render the same output as
    # pipeline.load_prompt for the templates that the prover uses (these
    # templates rely on {{...}} escapes intended for str.format).
    try:
        a = load_prompt(
            prompts_dir, "decomposition-prover/regulator.md",
            mode="DECIDE", state_file="x", decomposition_file="x",
            proof_file="x", verification_report="", attempt_history="",
            max_decompositions=4, max_revisions=4, max_proof_attempts=4,
            output_file="x", plan_history_file="x",
            verification_phase="structural",
        )
        b = decomp_load_prompt(
            prompts_dir, "decomposition-prover/regulator.md",
            mode="DECIDE", state_file="x", decomposition_file="x",
            proof_file="x", verification_report="", attempt_history="",
            max_decompositions=4, max_revisions=4, max_proof_attempts=4,
            output_file="x", plan_history_file="x",
            verification_phase="structural",
        )
        check("decomposition_prover.load_prompt matches pipeline.load_prompt",
              a == b, "Renderer divergence — escapes will not resolve at runtime")
        check("regulator.md {{N}} escape resolves to {N}",
              "{N}" in b and "{{N}}" not in b,
              "Literal '{{N}}' would be sent to the model")
    except Exception as e:
        check("decomposition_prover.load_prompt matches pipeline.load_prompt",
              False, str(e))

    # -------------------------------------------------------
    # Test 3c: Verdict parsing (code-only, fail-safe)
    # -------------------------------------------------------
    print("\n=== Test 3c: Verdict parsing (code-only) ===")
    with tempfile.TemporaryDirectory() as td:
        def _mk(name: str, body: str) -> str:
            p = os.path.join(td, name)
            write_text(p, body)
            return p

        parse_cases = [
            ("verdict_pass.md", "## Summary\n\n### Overall Verdict: PASS\n", "PASS"),
            ("verdict_fail.md", "## Summary\n\n### Overall Verdict: FAIL\n", "FAIL"),
            ("verdict_bold.md", "### **Overall Verdict:** PASS\n", "PASS"),
            ("verdict_structural.md", "### Overall Structural Verdict: PASS\n", "PASS"),
            # The trailing Summary is authoritative; earlier hits may be quotes.
            ("verdict_last_wins_pass.md",
             "### Overall Verdict: FAIL\n(quoted earlier)\n### Overall Verdict: PASS\n", "PASS"),
            ("verdict_last_wins_fail.md",
             "### Overall Verdict: PASS\n(quoted earlier)\n### Overall Verdict: FAIL\n", "FAIL"),
            ("verdict_placeholder.md", "### Overall Verdict: [PASS/FAIL]\n", "UNPARSEABLE"),
            ("verdict_none.md", "# Report\n\nNo verdict field here.\n", "UNPARSEABLE"),
            ("verdict_blank.md", "  \n\n", "MISSING"),
        ]
        for fname, body, expected in parse_cases:
            got = parse_report_verdict(_mk(fname, body))
            check(f"parse_report_verdict({fname}) -> {expected}",
                  got == expected, f"got {got}")

        check("parse_report_verdict(absent file) -> MISSING",
              parse_report_verdict(os.path.join(td, "does_not_exist.md")) == "MISSING",
              "an absent report must never count as passing")
        check("parse_report_verdict('') -> MISSING",
              parse_report_verdict("") == "MISSING")

        pass_report = _mk("ok.md", "### Overall Verdict: PASS\n")
        fail_report = _mk("bad.md", "### Overall Verdict: FAIL\n")
        check("compute_verdict(STRUCTURAL, PASS) -> DONE",
              compute_verdict("STRUCTURAL", pass_report) == "DONE")
        check("compute_verdict(STRUCTURAL, FAIL) -> CONTINUE",
              compute_verdict("STRUCTURAL", fail_report) == "CONTINUE")
        check("compute_verdict(STRUCTURAL, absent) -> CONTINUE",
              compute_verdict("STRUCTURAL", os.path.join(td, "nope.md")) == "CONTINUE")
        check("compute_verdict(FINAL, PASS, FAIL) -> CONTINUE",
              compute_verdict("FINAL", pass_report, fail_report) == "CONTINUE")
        check("compute_verdict(FINAL, PASS, PASS) -> DONE",
              compute_verdict("FINAL", pass_report, pass_report) == "DONE")
        check("compute_verdict(FINAL, PASS, '') -> CONTINUE",
              compute_verdict("FINAL", pass_report, "") == "CONTINUE",
              "FINAL must require the detailed report")

    # -------------------------------------------------------
    # Test 3d: Per-step proof checkpoints
    # -------------------------------------------------------
    print("\n=== Test 3d: Per-step proof checkpoints ===")
    with tempfile.TemporaryDirectory() as td:
        problem_file = os.path.join(td, "problem.tex")
        write_text(problem_file, "Prove that the integral of f vanishes.\n")

        proof_dir = os.path.join(td, "proof_1")
        checkpoint_dir = steps_dir(proof_dir)
        os.makedirs(checkpoint_dir)
        decomposition = {"proof_order": ["STEP1", "STEP2", "GOAL"]}
        body = "**Claim:** claim.\n\n**Proof:**\n" + ("argument. " * 40)

        def _write_step(step_id: str, text: str, heading: bool = True) -> None:
            head = f"### {step_id}: Checkpoint\n\n" if heading else ""
            write_text(os.path.join(checkpoint_dir, step_id + ".md"),
                       head + text + "\n\n**Dependencies:** none\n")

        check("assembler returns None when no checkpoint exists",
              assemble_proof_from_steps(proof_dir, problem_file, decomposition) is None,
              "an absent checkpoint must fall through to the old behaviour")

        for sid in ("STEP1", "STEP2", "GOAL"):
            _write_step(sid, body)
        write_text(os.path.join(checkpoint_dir, "00_front.md"),
                   "# Proof\n\n## Problem Statement\n\nProve it.\n\n## Proof\n")
        write_text(os.path.join(checkpoint_dir, "99_notes.md"),
                   "## Key Ideas\n\nDirect.\n")

        text, present, missing = assemble_proof_from_steps(
            proof_dir, problem_file, decomposition)
        check("complete checkpoint: nothing missing", missing == [], f"missing={missing}")
        check("complete checkpoint: kept in proof_order",
              text.index("### STEP1:") < text.index("### STEP2:")
              < text.index("### GOAL:"))
        check("complete checkpoint: front matter preserved", text.startswith("# Proof"))
        check("complete checkpoint: notes appended", "## Key Ideas" in text)
        check("complete checkpoint: no generated gap list",
              "## Unresolved Steps" not in text,
              "the model owns that section when the checkpoint is complete")

        # The session died before STEP2 and GOAL were written.
        for sid in ("STEP2", "GOAL"):
            os.remove(os.path.join(checkpoint_dir, sid + ".md"))
        write_text(os.path.join(checkpoint_dir, "99_notes.md"),
                   "## Unresolved Steps\n\nNone — all steps proved.\n\n"
                   "## Key Ideas\n\nDirect.\n")
        text, present, missing = assemble_proof_from_steps(
            proof_dir, problem_file, decomposition)
        check("partial checkpoint: missing listed in proof_order",
              missing == ["STEP2", "GOAL"], f"got {missing}")
        check("partial checkpoint: gap list generated",
              "## Unresolved Steps" in text and "**STEP2**" in text)
        check("partial checkpoint: stale model gap list dropped",
              text.count("## Unresolved Steps") == 1,
              "two contradictory lists would be worse than none")
        check("partial checkpoint: notes kept after the gap list",
              text.index("## Unresolved Steps") < text.index("## Key Ideas"))

        # A stub is not progress.
        write_text(os.path.join(checkpoint_dir, "STEP2.md"), "### STEP2: todo\n")
        _, _, missing = assemble_proof_from_steps(proof_dir, problem_file, decomposition)
        check("stub checkpoint counts as missing", "STEP2" in missing)

        # A checkpoint without a heading is invisible to the verifier's step scan.
        _write_step("STEP2", body, heading=False)
        text, _, missing = assemble_proof_from_steps(
            proof_dir, problem_file, decomposition)
        check("headingless checkpoint regains its heading",
              "### STEP2:" in text and "STEP2" not in missing)

        # No usable decomposition: natural order, one digit group per number,
        # so STEP10 follows STEP2 rather than preceding it.
        _write_step("GOAL", body)
        write_text(os.path.join(checkpoint_dir, "STEP10.md"), body)
        check("step_order fallback: STEP2 before STEP10, GOAL last",
              step_order(None, proof_dir) == ["STEP1", "STEP2", "STEP10", "GOAL"],
              f"got {step_order(None, proof_dir)}")

        # Front matter is regenerated when the model never wrote it.
        os.remove(os.path.join(checkpoint_dir, "00_front.md"))
        text, _, _ = assemble_proof_from_steps(proof_dir, problem_file, None)
        check("missing front matter is regenerated from the problem file",
              text.startswith("# Proof") and "integral of f vanishes" in text,
              "the verifier compares this section against the problem file")

    # -------------------------------------------------------
    # Test 3e: Carrying an interrupted attempt forward
    # -------------------------------------------------------
    print("\n=== Test 3e: Continuation of an interrupted proof ===")
    with tempfile.TemporaryDirectory() as td:
        decomposition = {"proof_order": ["STEP1", "STEP2", "GOAL"]}
        body = "**Claim:** claim.\n\n**Proof:**\n" + ("argument. " * 40)

        def _seed_attempt(directory: str, steps: dict[str, str],
                          proof_md: bool, notes: str = "") -> None:
            checkpoints = steps_dir(directory)
            os.makedirs(checkpoints)
            for sid, text in steps.items():
                write_text(os.path.join(checkpoints, sid + ".md"),
                           f"### {sid}: Checkpoint\n\n{text}\n")
            write_text(os.path.join(checkpoints, "00_front.md"), "# Proof\n\n## Proof\n")
            write_text(os.path.join(checkpoints, "99_notes.md"), notes)
            write_text(os.path.join(directory, "scratchpad.md"), "tried X on GOAL\n")
            if proof_md:
                write_text(os.path.join(directory, "proof.md"), "### STEP1:\nproof\n")

        # An attempt that wrote proof.md failed on its mathematics. Its steps
        # were judged, so they must not be inherited.
        judged = os.path.join(td, "proof_1")
        _seed_attempt(judged, {"STEP1": body}, proof_md=True)
        check("a judged attempt is not a continuation", interrupted_partial(judged) == "")
        new_dir = os.path.join(td, "proof_2")
        carried, missing = carry_forward_checkpoints(new_dir, judged, decomposition)
        check("judged steps are not carried forward", carried == [] and missing ==
              ["STEP1", "STEP2", "GOAL"], f"carried={carried} missing={missing}")

        # An attempt with no proof.md and no partial wrote nothing at all.
        empty = os.path.join(td, "proof_empty")
        os.makedirs(empty)
        check("an attempt that wrote nothing is not a continuation",
              interrupted_partial(empty) == "")

        # An interrupted attempt left a partial: continue from it.
        cut = os.path.join(td, "proof_cut")
        _seed_attempt(
            cut, {"STEP1": body, "STEP2": "### STEP2: todo\n"}, proof_md=False,
            notes="## Unresolved Steps\n\n- **GOAL** — not proved\n\n## Key Ideas\n\nDirect.\n",
        )
        write_text(os.path.join(cut, "proof_partial.md"), "# Proof\n\n### STEP1:\n...\n")
        check("an interrupted attempt is a continuation",
              interrupted_partial(cut) == os.path.join(cut, "proof_partial.md"))

        resumed = os.path.join(td, "proof_next")
        carried, missing = carry_forward_checkpoints(resumed, cut, decomposition)
        check("finished steps carried forward", carried == ["STEP1"], f"got {carried}")
        check("unfinished steps remain to write",
              missing == ["STEP2", "GOAL"], f"got {missing}")
        check("carried step file exists in the new attempt",
              os.path.exists(os.path.join(steps_dir(resumed), "STEP1.md")))
        check("stub is not carried as progress",
              not os.path.exists(os.path.join(steps_dir(resumed), "STEP2.md")))
        check("front matter carried forward",
              os.path.exists(os.path.join(steps_dir(resumed), "00_front.md")))
        check("scratchpad carried forward",
              "tried X on GOAL" in read_text(os.path.join(resumed, "scratchpad.md")),
              "it records the routes already tried on the missing steps")
        notes = read_text(os.path.join(steps_dir(resumed), "99_notes.md"))
        check("carried notes drop the dead session's gap list",
              "## Unresolved Steps" not in notes and "## Key Ideas" in notes,
              "it describes steps this attempt may still prove")

        # The new attempt now stands on its own: if it is cut off too, it still
        # assembles from its own directory.
        problem = os.path.join(td, "problem.tex")
        write_text(problem, "Prove that the integral of f vanishes.\n")
        text, present, still_missing = assemble_proof_from_steps(
            resumed, problem, decomposition)
        check("a continuation assembles from its own directory",
              present == ["STEP1"] and still_missing == ["STEP2", "GOAL"],
              f"present={present} missing={still_missing}")

        # Resuming twice must not clobber what the current session already wrote.
        write_text(os.path.join(steps_dir(resumed), "STEP2.md"),
                   "### STEP2: Checkpoint\n\n" + body)
        carried, missing = carry_forward_checkpoints(resumed, cut, decomposition)
        check("a resumed session's own file is never overwritten",
              carried == ["STEP1", "STEP2"] and missing == ["GOAL"],
              f"carried={carried} missing={missing}")
        check("the carried file keeps the current session's text",
              "### STEP2: Checkpoint" in
              read_text(os.path.join(steps_dir(resumed), "STEP2.md")))

    # -------------------------------------------------------
    # Test 3f: Failure classification
    # -------------------------------------------------------
    print("\n=== Test 3f: Failure classification ===")
    import model_runner as mr

    class _CliResult:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    real_run_cli_async = mr.run_cli_async

    def stub_cli(result):
        async def _fake(cmd, **kwargs):
            return result
        mr.run_cli_async = _fake

    try:
        # The exact shape of the four real crashes in run_20261023_103103:
        # exit 1, an empty or single-line stderr, and no usage event.
        category, evidence = mr.classify_failure(
            "codex", stdout="",
            stderr="Reading additional input from stdin...",
            returncode=1, completed=False)
        check("a dead session is not called token exhaustion",
              category == mr.SESSION_DIED, category)
        check("the exit code is kept as evidence",
              any("exit code 1" in e for e in evidence), evidence)

        category, _ = mr.classify_failure(
            "codex",
            error_events=["turn.failed: context_length_exceeded: requested 412000 > 400000"],
            returncode=0, completed=False)
        check("an overflow event is token exhaustion",
              category == mr.TOKEN_CONTEXT, category)

        category, _ = mr.classify_failure(
            "gemini", error_events=["error 429: rate limit exceeded"],
            returncode=0, completed=False)
        check("a quota message is quota", category == mr.QUOTA, category)

        category, _ = mr.classify_failure(
            "codex", stdout="the constant is 14291", returncode=1, completed=False)
        check("429 does not match inside an ordinary number",
              category == mr.SESSION_DIED, category)

        with tempfile.TemporaryDirectory() as td:
            # claude reports failures inside a zero-exit JSON envelope; without
            # handling it the error string becomes the agent's "response".
            stub_cli(_CliResult(stdout=json.dumps({
                "type": "result", "subtype": "error_max_budget_usd",
                "is_error": True, "result": "Error: max budget reached"})))
            try:
                await mr.run_claude_agent("p", td, {"model": "m"}, call_name="verify")
                check("a claude budget ceiling raises", False)
            except TokenExhausted as exc:
                check("a spend ceiling is quota", exc.category == mr.QUOTA, exc.category)
                check("the message leads with the verdict",
                      str(exc).startswith("TOKEN EXHAUSTED"), str(exc)[:40])
                check("the raw output is preserved",
                      bool(exc.raw_log) and os.path.exists(exc.raw_log), exc.raw_log)

            stub_cli(_CliResult(stdout=json.dumps({
                "type": "result", "subtype": "error_during_execution",
                "is_error": True, "result": "prompt is too long: 410000 tokens"})))
            try:
                await mr.run_claude_agent("p", td, {"model": "m"}, call_name="prover")
                check("a claude context overflow raises", False)
            except TokenExhausted as exc:
                check("the overflow is attributed to the context window",
                      exc.category == mr.TOKEN_CONTEXT, exc.category)

            stub_cli(_CliResult(stdout=json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "result": "done", "modelUsage": {}})))
            text = await mr.run_claude_agent("p", td, {"model": "m"}, call_name="ok")
            check("a successful claude call still returns its text", text == "done", text)

            # A failed turn is not an answer, even when it narrated something
            # first: that narration is what made the 93-minute hang look like
            # progress.
            stub_cli(_CliResult(stdout="\n".join([
                json.dumps({"type": "item.completed",
                            "item": {"type": "agent_message", "text": "Let me check."}}),
                json.dumps({"type": "turn.failed",
                            "error": {"message": "context_length_exceeded"}}),
            ])))
            try:
                await mr.run_codex_agent("p", td, {"model": "m"}, call_name="prover")
                check("a failed codex turn raises instead of returning narration", False)
            except TokenExhausted as exc:
                check("codex turn.failed maps to the context window",
                      exc.category == mr.TOKEN_CONTEXT, exc.category)

            stub_cli(_CliResult(stdout="", stderr="", returncode=1))
            try:
                await mr.run_codex_agent("p", td, {"model": "m"}, call_name="prover")
                check("a codex crash raises", False)
            except TokenExhausted:
                check("a silent crash is not blamed on tokens", False)
            except ModelRunnerError as exc:
                check("a silent crash stays a session death",
                      exc.category == mr.SESSION_DIED, exc.category)
    finally:
        mr.run_cli_async = real_run_cli_async

    # -------------------------------------------------------
    # Test 3g: Halt report
    # -------------------------------------------------------
    print("\n=== Test 3g: Halt report ===")
    with tempfile.TemporaryDirectory() as td:
        revision = os.path.join(td, "decomposition", "attempt_1", "revision_2")
        proof_dir = os.path.join(revision, "proof_3")
        checkpoint_dir = steps_dir(proof_dir)
        os.makedirs(checkpoint_dir)
        write_text(os.path.join(revision, "decomposition.yaml"),
                   "proof_order: [STEP1, STEP2]\n")
        write_text(os.path.join(checkpoint_dir, "00_front.md"), "# Proof\n\n## Proof\n")
        write_text(os.path.join(checkpoint_dir, "STEP1.md"),
                   "### STEP1: First\n\n**Claim:** c.\n\n**Proof:**\n"
                   + "argument. " * 60)
        write_text(os.path.join(td, "problem.tex"), "Prove something.\n")

        status_file = os.path.join(td, "decomposition", "STATUS.md")
        DecompositionLogger(td).update_status(
            state="PROVING", attempt=1, revision=2, proof=3,
            recent_activity="single prover running")
        DecompositionLogger(td)     # reopening must not reset what is on disk
        after_reopen = read_text(status_file)
        check("reopening the logger keeps the run's position",
              "| Attempt | 1 |" in after_reopen and "| Revision | 2 |" in after_reopen
              and "| Proof | 3 |" in after_reopen, after_reopen)

        exc = TokenExhausted(
            provider="codex", error_type=mr.TOKEN_CONTEXT, message="x",
            category=mr.TOKEN_CONTEXT,
            evidence=["turn.failed: context_length_exceeded"], raw_log="/tmp/raw.log",
            model="gpt", call_name="single_prover_a1_r2_p3", elapsed=5610.8)
        report = halt_run(td, exc, problem_file=os.path.join(td, "problem.tex"))

        check("the halt is classified", report["category"] == mr.TOKEN_CONTEXT)
        check("the halt reports the working position",
              (report["attempt"], report["revision"], report["proof"]) == (1, 2, 3),
              report)
        check("a halt does not consume a proof attempt",
              report["attempts_consumed"] is False)
        check("finished steps are assembled into a partial",
              bool(report["proof_partial"]) and os.path.exists(report["proof_partial"]),
              report["proof_partial"])
        # Without proof_partial.md, P1 cannot recognise this as resumable; the
        # checkpoints would sit on disk and nothing would mark them.
        check("the partial is what the resume rule looks for",
              interrupted_partial(proof_dir) == report["proof_partial"],
              interrupted_partial(proof_dir))
        check("the missing steps are named",
              report["checkpoints_missing"] == ["STEP2"], report["checkpoints_missing"])

        halt_md = read_text(os.path.join(td, "HALT.md"))
        check("HALT.md leads with the verdict",
              "RUN HALTED — TOKEN EXHAUSTED" in halt_md, halt_md[:60])
        check("HALT.md points at the raw output", "/tmp/raw.log" in halt_md)
        check("HALT.md names the resume command", "python code/run.py" in halt_md)

        halted_status = read_text(status_file)
        check("STATUS.md is set to HALTED", "HALTED" in halted_status, halted_status)
        check("STATUS.md does not reset the counters",
              "| Proof | 3 |" in halted_status and "| Revision | 2 |" in halted_status,
              halted_status)
        check("halt.json is machine readable",
              json.loads(read_text(os.path.join(td, "halt.json")))["category"]
              == mr.TOKEN_CONTEXT)
        check("the halt exit code stays distinct from a crash", HALT_EXIT_CODE == 3)

    # -------------------------------------------------------
    # Test 4: Skill loading
    # -------------------------------------------------------
    print("\n=== Test 4: Skill loading ===")
    math_skill_path = os.path.join(skill_dir, "super_math_skill.md")
    try:
        with open(math_skill_path, encoding="utf-8") as f:
            math_skill = f.read()
        check("Math skill loads", len(math_skill) > 100)
    except Exception as e:
        check("Skill loading", False, str(e))

    # -------------------------------------------------------
    # Determine which providers are actually used by the configured pipeline.
    # Skip connectivity tests for providers that no role references.
    # -------------------------------------------------------
    import shutil
    import subprocess

    pipeline_cfg_for_use = config.get("pipeline", {})
    providers_in_use: set[str] = set()

    def _role_provider(role_cfg) -> str | None:
        if isinstance(role_cfg, dict):
            p = role_cfg.get("provider")
            return p.lower() if isinstance(p, str) else None
        return None

    for agent_key in ("literature_survey", "proof_summary"):
        prov = _role_provider(pipeline_cfg_for_use.get(agent_key))
        if prov:
            providers_in_use.add(prov)

    decomp_models = config.get("decomposition", {}).get("models", {})
    for v in decomp_models.values():
        prov = _role_provider(v)
        if prov:
            providers_in_use.add(prov)

    providers_in_use = {p for p in providers_in_use if p in {"claude", "codex", "gemini"}}

    # -------------------------------------------------------
    # Test 5: Claude CLI connectivity (provider-aware)
    # -------------------------------------------------------
    provider = claude_cfg.get("provider", "subscription")
    run_claude_test = "claude" in providers_in_use
    if not run_claude_test:
        print("\n=== Test 5: Claude CLI connectivity [SKIPPED — Claude not used by any role] ===")
    else:
        print(f"\n=== Test 5: Claude CLI connectivity (provider: {provider}) ===")

    # Detect if ~/.claude/settings.json injects provider vars that would
    # override the config.yaml provider selection.
    _PROVIDER_VARS = ("CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY",
                      "AWS_PROFILE", "ANTHROPIC_MODEL")
    cli_settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    cli_settings_conflict = False
    if run_claude_test and os.path.exists(cli_settings_path):
        import json as _json_settings
        try:
            with open(cli_settings_path, encoding="utf-8") as _sf:
                cli_settings = _json_settings.load(_sf)
            settings_env = cli_settings.get("env", {})
            if provider == "subscription" and settings_env.get("CLAUDE_CODE_USE_BEDROCK"):
                check("No settings.json provider conflict", False,
                      "~/.claude/settings.json has CLAUDE_CODE_USE_BEDROCK set — "
                      "the CLI will use Bedrock regardless of config.yaml provider='subscription'. "
                      "Either set provider to 'bedrock' in config.yaml, or remove "
                      "CLAUDE_CODE_USE_BEDROCK from ~/.claude/settings.json")
                cli_settings_conflict = True
            elif provider == "subscription" and settings_env.get("ANTHROPIC_API_KEY"):
                check("No settings.json provider conflict", False,
                      "~/.claude/settings.json has ANTHROPIC_API_KEY set — "
                      "the CLI will use API key auth regardless of config.yaml provider='subscription'. "
                      "Either set provider to 'api_key' in config.yaml, or remove "
                      "ANTHROPIC_API_KEY from ~/.claude/settings.json")
                cli_settings_conflict = True
            elif provider == "api_key" and settings_env.get("CLAUDE_CODE_USE_BEDROCK"):
                check("No settings.json provider conflict", False,
                      "~/.claude/settings.json has CLAUDE_CODE_USE_BEDROCK set — "
                      "the CLI may use Bedrock instead of your API key. "
                      "Remove CLAUDE_CODE_USE_BEDROCK from ~/.claude/settings.json")
                cli_settings_conflict = True
            else:
                check("No settings.json provider conflict", True)
        except Exception:
            pass  # Can't read settings — not a conflict we can detect

    cli_path = claude_cfg.get("cli_path", "claude")
    if not run_claude_test:
        pass
    elif shutil.which(cli_path) is not None:
        check(f"Claude CLI '{cli_path}' found", True)

        # Build options via make_claude_options so we get the right model
        # and env vars for the configured provider.
        try:
            opts = make_claude_options(claude_cfg, tempfile.gettempdir())
        except ValueError as e:
            check(f"Claude config valid ({provider})", False, str(e))
            opts = None

        if opts is not None:
            check(f"Claude config valid ({provider})", True)

            # Strip vars that cause provider cross-contamination, then
            # add back only the ones for the configured provider.
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in _PROVIDER_VARS}
            clean_env.update(opts["env"])

            model = opts["model"]
            try:
                result = subprocess.run(
                    [resolve_cli(cli_path), "-p", "--output-format", "json",
                     "--model", model,
                     "Reply with exactly: SMOKE_TEST_OK"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=clean_env, timeout=60, **decode_kwargs(),
                )
                data = json.loads(result.stdout)
                text = data.get("result", "")
                check("Claude CLI exits successfully",
                      result.returncode == 0 and not data.get("is_error", False),
                      f"Exit code {result.returncode}, stderr: {result.stderr[:200]}")
                check(f"Claude CLI responds ({provider}, model={model})",
                      len(text) > 0,
                      f"Empty response, stderr: {result.stderr[:200]}")
                check("Claude CLI response valid",
                      text.strip() == "SMOKE_TEST_OK",
                      f"Got: {text[:100]}")
            except subprocess.TimeoutExpired:
                check(f"Claude CLI responds ({provider})", False, "Timed out after 60s")
            except Exception as e:
                check(f"Claude CLI connectivity ({provider})", False, str(e))
    else:
        check(f"Claude CLI '{cli_path}' found", False,
              "Install Claude Code using the instructions in README.md")

    # -------------------------------------------------------
    # Test 6: Config validation
    # -------------------------------------------------------
    print("\n=== Test 6: Config validation ===")
    pipeline_cfg = config.get("pipeline", {})
    check("claude config present", "claude" in config, "Missing claude config")

    prover_cfg = config.get("prover", {})
    prover_mode = prover_cfg.get("mode", "decomposition")
    check("prover.mode is 'decomposition'", prover_mode == "decomposition",
          f"Only 'decomposition' is supported; got: {prover_mode}")

    print("\n=== Test 6b: Decomposition config validation ===")
    decomp_cfg = config.get("decomposition", {})
    check("decomposition config present", bool(decomp_cfg),
          "Missing decomposition config")

    valid_providers = {"claude", "codex", "gemini"}

    def _validate_role(label: str, role_cfg) -> None:
        if not isinstance(role_cfg, dict):
            check(f"{label} is a dict", False,
                  f"Expected dict like {{provider: 'codex', model: 'gpt-5.5'}}, got: {role_cfg!r}")
            return
        check(f"{label} is a dict", True)
        provider = role_cfg.get("provider")
        ok = isinstance(provider, str) and provider.lower() in valid_providers
        check(f"{label}.provider valid ({provider})",
              ok, f"Invalid or missing provider for {label}")

    # Validate decomposition model assignments
    decomp_models = decomp_cfg.get("models", {})
    required_agents = ["decomposer", "single_prover", "regulator",
                      "structural_verifier", "detailed_verifier"]
    for agent in required_agents:
        _validate_role(f"decomposition.models.{agent}", decomp_models.get(agent))

    # Validate limits
    max_proof = decomp_cfg.get("max_proof_attempts", 3)
    max_rev = decomp_cfg.get("max_revisions", 2)
    max_decomp = decomp_cfg.get("max_decompositions", 2)
    check("max_proof_attempts > 0", max_proof > 0, f"Invalid: {max_proof}")
    check("max_revisions > 0", max_rev > 0, f"Invalid: {max_rev}")
    check("max_decompositions > 0", max_decomp > 0, f"Invalid: {max_decomp}")

    # -------------------------------------------------------
    # Test 7: Auxiliary agent providers config validation
    # -------------------------------------------------------
    print("\n=== Test 7: Auxiliary agent providers ===")
    for agent_key in ("literature_survey", "proof_summary"):
        _validate_role(f"pipeline.{agent_key}", pipeline_cfg.get(agent_key))

    # -------------------------------------------------------
    # Test 7b: Standalone verifier agents (if present)
    # -------------------------------------------------------
    sv_cfg = config.get("standalone_verifier", {})
    if sv_cfg:
        print("\n=== Test 7b: Standalone verifier agents ===")
        for agent_key in ("judge", "structural_verifier", "detailed_verifier", "problem_reviewer"):
            _validate_role(f"standalone_verifier.{agent_key}", sv_cfg.get(agent_key))

    # -------------------------------------------------------
    # Test 8: Non-Claude provider connectivity (when needed)
    # -------------------------------------------------------
    providers_to_test = {p for p in providers_in_use if p != "claude"}

    if providers_to_test:
        print(f"\n=== Test 8: Non-Claude provider connectivity (testing: {', '.join(sorted(providers_to_test))}) ===")
        import json as _json_test

        # --- Codex ---
        if "codex" in providers_to_test:
            codex_cfg = config.get("codex", {})
            codex_cli = codex_cfg.get("cli_path", "codex")
            if shutil.which(codex_cli) is not None:
                try:
                    codex_model = codex_cfg.get("model", "gpt-6-astra")
                    codex_reasoning = codex_cfg.get("reasoning_effort", "max")
                    # Mirror model_runner.run_codex_agent so the exit-code
                    # check reflects how Codex behaves at runtime.
                    with tempfile.TemporaryDirectory(prefix="qed-codex-smoke-") as codex_cwd:
                        codex_result = subprocess.run(
                            [resolve_cli(codex_cli), "--search", "-m", codex_model,
                             "-c", f'model_reasoning_effort="{codex_reasoning}"',
                             "exec", "--json",
                             "--skip-git-repo-check",
                             "--dangerously-bypass-approvals-and-sandbox",
                             "-C", codex_cwd,
                             "Reply with exactly: SMOKE_TEST_OK"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=120, cwd=codex_cwd, **decode_kwargs(),
                        )
                    codex_resp = ""
                    for line in codex_result.stdout.splitlines():
                        if not line.strip():
                            continue
                        event = _json_test.loads(line)
                        if event.get("type") == "item.completed":
                            item = event.get("item", {})
                            if item.get("type") == "agent_message":
                                codex_resp = item.get("text", "").strip()
                    # Non-zero exit is treated as a warning if a response was
                    # produced (matches run_codex_agent's tolerance).
                    exit_ok = codex_result.returncode == 0 or bool(codex_resp)
                    check("Codex CLI exits cleanly (or returned a response)",
                          exit_ok,
                          f"Exit code {codex_result.returncode}, stderr: {codex_result.stderr[:200]}")
                    check("Codex responds", len(codex_resp) > 0, "Empty response")
                    check("Codex response valid",
                          codex_resp == "SMOKE_TEST_OK",
                          f"Got: {codex_resp[:100]}")
                except subprocess.TimeoutExpired:
                    check("Codex connectivity", False, "Timed out after 120s")
                except Exception as e:
                    check("Codex connectivity", False, str(e))
            else:
                check(f"Codex CLI '{codex_cli}' found", False,
                      "Install codex or switch decomposition.models.* away from codex")

        # --- Gemini ---
        if "gemini" in providers_to_test:
            gemini_cfg = config.get("gemini", {})
            gemini_cli = gemini_cfg.get("cli_path", "gemini")
            gemini_api_key = gemini_cfg.get("api_key", "")
            if shutil.which(gemini_cli) is not None:
                try:
                    gemini_model = gemini_cfg.get("model", "gemini-3.1-pro-preview")
                    gemini_approval_mode = gemini_cfg.get("approval_mode", "yolo")
                    gemini_thinking_level = gemini_cfg.get("thinking_level", "")
                    gemini_thinking_budget = gemini_cfg.get("thinking_budget")
                    gemini_env = os.environ.copy()
                    if gemini_api_key:
                        gemini_env["GEMINI_API_KEY"] = gemini_api_key

                    thinking_config = {}
                    if gemini_thinking_level:
                        thinking_config["thinkingLevel"] = gemini_thinking_level
                    if gemini_thinking_budget is not None:
                        thinking_config["thinkingBudget"] = gemini_thinking_budget

                    if thinking_config:
                        with tempfile.TemporaryDirectory(prefix="qed-gemini-home-") as gemini_home:
                            settings_dir = os.path.join(gemini_home, ".gemini")
                            os.makedirs(settings_dir, exist_ok=True)
                            settings_path = os.path.join(settings_dir, "settings.json")
                            settings = {
                                "modelConfigs": {
                                    "overrides": [
                                        {
                                            "match": {"model": gemini_model},
                                            "modelConfig": {
                                                "generateContentConfig": {
                                                    "thinkingConfig": thinking_config,
                                                }
                                            },
                                        }
                                    ]
                                }
                            }
                            with open(settings_path, "w", encoding="utf-8") as f:
                                json.dump(settings, f)
                            gemini_env["GEMINI_CLI_HOME"] = gemini_home
                            gemini_result = subprocess.run(
                                [resolve_cli(gemini_cli), "-m", gemini_model, "--approval-mode", gemini_approval_mode, "-o", "json",
                                 "-p", "Reply with exactly: SMOKE_TEST_OK"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=120, env=gemini_env, **decode_kwargs(),
                            )
                    else:
                        gemini_result = subprocess.run(
                            [resolve_cli(gemini_cli), "-m", gemini_model, "--approval-mode", gemini_approval_mode, "-o", "json",
                             "-p", "Reply with exactly: SMOKE_TEST_OK"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=120, env=gemini_env, **decode_kwargs(),
                        )
                    check("Gemini CLI exits cleanly", gemini_result.returncode == 0,
                          f"Exit code {gemini_result.returncode}, stderr: {gemini_result.stderr[:300]}")
                    # Parse JSON response
                    gemini_data = _json_test.loads(gemini_result.stdout)
                    gemini_resp = gemini_data.get("response", "")
                    check("Gemini responds", len(gemini_resp) > 0,
                          f"Empty response, stdout: {gemini_result.stdout[:200]}")
                    check("Gemini response valid",
                          not gemini_data.get("error") and gemini_resp.strip() == "SMOKE_TEST_OK",
                          f"Got: {gemini_resp[:100]}")
                except subprocess.TimeoutExpired:
                    check("Gemini connectivity", False, "Timed out after 120s")
                except Exception as e:
                    check("Gemini connectivity", False, str(e))
            else:
                check(f"Gemini CLI '{gemini_cli}' found", False,
                      "Install gemini or switch decomposition.models.* away from gemini")
    else:
        print("\n=== Test 8: Non-Claude provider connectivity [SKIPPED — no non-Claude providers enabled] ===")

    # -------------------------------------------------------
    # Summary
    # -------------------------------------------------------
    print(f"\n{'=' * 60}")
    print(f"SMOKE TEST RESULTS: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")

    return failed == 0


async def main():
    parser = argparse.ArgumentParser(description="Smoke test for the proof agent pipeline")
    parser.add_argument("--config", help="Path to config.yaml", default=None)
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_base = os.path.dirname(script_dir)
    config_path = args.config or os.path.join(project_base, "config.yaml")

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    ok = await run_smoke_test(config, config_path)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
