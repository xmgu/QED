#!/usr/bin/env python3
"""Decomposition-based prover for the QED pipeline.

This module implements a structured proof workflow that:
1. Decomposes a problem into a proof plan (intermediate steps)
2. Single prover writes a complete proof following the plan
3. Structural verification (Phases 1-5): problem integrity, completeness, citations, subgoal tree
4. Detailed verification (Phase 6): step-by-step logical analysis
5. Regulator decides next action on failure: REVISE_PROOF, REVISE_PLAN, or REWRITE

Pipeline flow:
    Decomposer → Single Prover → Structural Verifier → Detailed Verifier
         ↑                                                    ↓
         └──────────────────── Regulator ←────────────────────┘

This is the Stage 1 prover orchestrated by pipeline.py.
"""

import asyncio
import json
import os
import re
import shutil
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any

from model_runner import run_model, run_model_for_agent, run_claude_agent, ModelRunnerError
from compat import read_text, write_text


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "max_proof_attempts": 3,     # REVISE_PROOF limit: proof attempts per revision
    "max_revisions": 2,          # REVISE_PLAN limit: plan revisions per attempt
    "max_decompositions": 3,     # REWRITE limit: total decomposition attempts
}

DEFAULT_MODELS = {
    "decomposer": {"provider": "codex"},
    "single_prover": {"provider": "codex"},
    "regulator": {"provider": "codex"},
    "structural_verifier": {"provider": "codex"},
    "detailed_verifier": {"provider": "codex"},
    # NOTE: no "verdict" role. Verdicts are computed in code (see
    # parse_report_verdict / compute_verdict), not by a model.
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def load_prompt(prompts_dir: str, name: str, **kwargs) -> str:
    """Load a prompt template and fill placeholders via ``str.format``.

    Uses the same renderer as ``pipeline.load_prompt`` so that ``{{...}}``
    escapes in the templates (used for literal braces inside LaTeX/math
    examples) are correctly unescaped to ``{...}`` in the rendered prompt.
    """
    path = os.path.join(prompts_dir, name)
    with open(path, encoding="utf-8") as f:
        template = f.read()
    return template.format(**kwargs)


def read_file(path: str) -> str:
    """Read file contents, return empty string if not found.

    Lenient decoding: the files read here are produced by the agents and
    may carry the locale code page rather than UTF-8.
    """
    return read_text(path)


def write_file(path: str, content: str) -> None:
    """Write content to file, creating directories as needed."""
    write_text(path, content)


def parse_decomposition(yaml_content: str) -> dict:
    """Parse decomposition YAML from agent response.

    Extracts the YAML block from markdown code fences if present.
    """
    yaml_content = _extract_yaml_block(yaml_content)
    try:
        return yaml.safe_load(yaml_content)
    except yaml.YAMLError as exc:
        repaired = _repair_yaml_escapes(yaml_content)
        if repaired == yaml_content:
            raise
        # LaTeX in a double-quoted scalar is the usual culprit: "\mathbb{E}"
        # and "A\B" are not valid escape sequences. Try again with the
        # offending backslashes doubled.
        try:
            return yaml.safe_load(repaired)
        except yaml.YAMLError:
            raise exc


def _extract_yaml_block(yaml_content: str) -> str:
    """Return the YAML document from a markdown code fence, if present."""
    if "```yaml" in yaml_content:
        start = yaml_content.find("```yaml") + 7
        end = yaml_content.find("```", start)
        return yaml_content[start:end].strip()
    if "```" in yaml_content:
        start = yaml_content.find("```") + 3
        end = yaml_content.find("```", start)
        return yaml_content[start:end].strip()
    return yaml_content.strip()


#: The escape sequences YAML defines inside a double-quoted scalar.
_YAML_ESCAPES = set('"\\/bfnrtv0abxeN_LP U')


def _repair_yaml_escapes(text: str) -> str:
    """Double the backslashes that YAML would reject inside a quoted scalar.

    A double-quoted scalar treats ``\\`` as an escape, so a LaTeX
    fragment such as ``\\mathbb{E}`` or ``A\\B`` does not parse. The
    backslashes are doubled only inside double-quoted scalars, and only
    where they do not already start a valid escape, so the rest of the
    document is left untouched.
    """
    out = []
    in_double = False
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if in_double:
            if ch == "\\" and i + 1 < n:
                nxt = text[i + 1]
                if nxt in _YAML_ESCAPES:
                    out.append(ch)
                    out.append(nxt)
                    i += 2
                    continue
                # An invalid escape: double the backslash so the literal
                # backslash survives into the parsed value.
                out.append("\\\\")
                i += 1
                continue
            if ch == '"':
                in_double = False
            out.append(ch)
            i += 1
            continue

        if ch == '"':
            in_double = True
        elif ch == "#" and i > 0 and text[i - 1] in " \t":
            # A comment runs to the end of the line; a double quote inside
            # it does not open a scalar.
            end = text.find("\n", i)
            if end == -1:
                end = n
            out.append(text[i:end])
            i = end
            continue
        out.append(ch)
        i += 1

    return "".join(out)


# ---------------------------------------------------------------------------
# Proof document validation
# ---------------------------------------------------------------------------

_STEP_HEADING_RE = re.compile(r"^#{1,6}\s*STEP\s*\d", re.MULTILINE | re.IGNORECASE)
_PROOF_TITLE_RE = re.compile(r"^#{1,6}\s*(?:The\s+)?Proof\b", re.MULTILINE | re.IGNORECASE)

# A document this long that calls itself a proof is accepted even without STEP
# headings, to tolerate the prover drifting from the mandated section layout.
_MIN_PROOF_CHARS = 1500


class ProofNotProduced(RuntimeError):
    """The prover finished without writing a proof document.

    Raised when the agent leaves ``proof.md`` unwritten *and* its chat response
    is not a proof either. This is what an interrupted session looks like: the
    agent narrates what it is about to do ("Let me write the proof next"), the
    session ends before the write, and the last message is all that survives.
    """


def looks_like_proof(text: str) -> bool:
    """Whether *text* is a proof document rather than incidental prose.

    The prover prompt requires one ``### STEP_ID:`` heading per decomposition
    step, and structural verifier Phase 4 keys off exactly that structure, so a
    response containing no such heading at all is narration, not a proof.
    """
    if not text or not text.strip():
        return False
    if _STEP_HEADING_RE.search(text):
        return True
    return bool(_PROOF_TITLE_RE.search(text)) and len(text) >= _MIN_PROOF_CHARS


class ProofIncomplete(ProofNotProduced):
    """The prover wrote part of the proof, then the session ended.

    The step checkpoints under ``proof_steps/`` cover *some* of ``proof_order``.
    Unlike a mathematical failure, there is nothing here for a verifier to judge:
    the remaining steps were never attempted. The written part is preserved as
    ``proof_partial.md`` so the loss is bounded and the cause is legible.
    """

    def __init__(self, message: str, present: list[str], missing: list[str],
                 partial_file: str) -> None:
        super().__init__(message)
        self.present = present
        self.missing = missing
        self.partial_file = partial_file


# ---------------------------------------------------------------------------
# Per-step proof checkpoints
# ---------------------------------------------------------------------------
#
# The prover is asked to write each step to its own file the moment the step is
# finished, so a session that dies mid-proof still leaves everything it managed
# to do. Rebuilding proof.md from those files happens here rather than in the
# prompt: it is deterministic, and asking the model to concatenate its own
# output would re-emit the whole proof (~21k tokens for an 85 KB document).

_STEPS_DIRNAME = "proof_steps"

# A checkpoint below this size is a stub, not a proof of the step. Real ones run
# to thousands of characters; the threshold only separates "wrote something"
# from "wrote a placeholder", so a stub counts as missing, not as progress.
_MIN_STEP_CHARS = 200

# Step ids come from the decomposition and become file names, so they must not
# be able to escape the directory.
_STEP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

_PROOF_H1_RE = re.compile(r"^#\s+Proof\b", re.MULTILINE | re.IGNORECASE)
_PROOF_H2_RE = re.compile(r"^##\s+Proof\b", re.MULTILINE | re.IGNORECASE)

# One whole "## Unresolved Steps" section, up to the next level-2 heading. Used
# to drop the model's copy when the pipeline generates its own.
_UNRESOLVED_SECTION_RE = re.compile(
    r"^##\s+Unresolved Steps\b.*?(?=^##\s|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL,
)


def looks_like_step(text: str) -> bool:
    """Whether a checkpoint file holds a step's argument rather than a stub."""
    return bool(text) and len(text.strip()) >= _MIN_STEP_CHARS


def steps_dir(proof_dir: str) -> str:
    """Directory holding this attempt's per-step checkpoints."""
    return os.path.join(proof_dir, _STEPS_DIRNAME)


def _natural_step_key(step_id: str):
    """Sort STEP2 before STEP10, with GOAL last for the no-plan fallback."""
    parts = re.split(r"(\d+)", step_id.upper())
    return (
        1 if step_id.upper() == "GOAL" else 0,
        [int(p) if p.isdigit() else p for p in parts],
    )


def step_order(decomposition: dict | None, proof_dir: str) -> list[str]:
    """The ids a complete proof must cover, in the order they must appear.

    ``proof_order`` from the decomposition is authoritative. File-name order is
    wrong in two ways: GOAL sorts before STEP alphabetically, and STEP10 before
    STEP2. Real plans also use suffixed ids (STEP7a, STEP7b), which is a further
    reason to copy ids verbatim instead of parsing them.
    """
    order: list[str] = []
    if isinstance(decomposition, dict):
        order = [str(s).strip() for s in (decomposition.get("proof_order") or [])]
    order = [s for s in order if _STEP_ID_RE.match(s)]
    if order:
        return order

    # No usable plan (corrupt YAML, hand-built directory): trust the checkpoints
    # that exist, in natural order.
    directory = steps_dir(proof_dir)
    try:
        names = [os.path.splitext(f)[0] for f in os.listdir(directory)
                 if f.endswith(".md")]
    except OSError:
        return []
    return sorted((n for n in names if _STEP_ID_RE.match(n)), key=_natural_step_key)


def collect_step_files(proof_dir: str, decomposition: dict | None):
    """Read the step checkpoints.

    Returns ``(present, missing, contents)``: the ids in ``proof_order`` that
    hold a real argument, those that do not (absent *or* a stub), and a map from
    id to text for the former.
    """
    directory = steps_dir(proof_dir)
    present: list[str] = []
    missing: list[str] = []
    contents: dict[str, str] = {}
    for step_id in step_order(decomposition, proof_dir):
        text = read_file(os.path.join(directory, step_id + ".md"))
        if looks_like_step(text):
            present.append(step_id)
            contents[step_id] = text
        else:
            missing.append(step_id)
    return present, missing, contents


def _front_matter(proof_dir: str, problem_file: str) -> str:
    """Title, problem statement, and the ``## Proof`` heading steps sit under."""
    front = read_file(os.path.join(steps_dir(proof_dir), "00_front.md")).strip()
    if not front:
        # Cheaper and more reliable than trusting a partly-written checkpoint:
        # the problem statement is copied from the file the verifier compares
        # against, so Phase 1 cannot fail on a paraphrase.
        problem = read_file(problem_file).strip()
        front = ("# Proof\n\n## Problem Statement\n\n"
                 + (problem or "[problem file unavailable]"))
    if not _PROOF_H1_RE.search(front):
        front = "# Proof\n\n" + front
    if not _PROOF_H2_RE.search(front):
        front += "\n\n## Proof"
    return front


def _unresolved_section(missing: list[str]) -> str:
    """The gap list, generated from the file listing rather than from a model."""
    lines = [
        "## Unresolved Steps",
        "",
        "The proof session ended before these steps were written. They are "
        "listed here so the gap is explicit rather than hidden:",
        "",
    ]
    for step_id in missing:
        lines += [
            f"- **{step_id}** — not proved",
            "  - **Tried:** the session ended before this step was written",
            "  - **Breaks at:** no argument for this step exists",
        ]
    return "\n".join(lines)


def assemble_proof_from_steps(proof_dir: str, problem_file: str,
                              decomposition: dict | None):
    """Rebuild the proof from its per-step checkpoints.

    Returns ``(text, present, missing)``, or ``None`` when no checkpoint holds a
    real argument. ``missing`` is what separates "the session died here" from
    "the mathematics is incomplete": the prompt requires a file for every id, so
    an unprovable step is a file that says so, not an absent file.
    """
    present, missing, contents = collect_step_files(proof_dir, decomposition)
    if not present:
        return None

    chunks = []
    for step_id in present:
        chunk = contents[step_id].strip()
        if not re.match(r"^#{1,6}\s*\S", chunk):
            # A checkpoint without any heading cannot be located by the
            # verifier's step scan; restore the heading the prompt mandates.
            chunk = f"### {step_id}:\n\n" + chunk
        chunks.append(chunk)

    parts = [_front_matter(proof_dir, problem_file), "\n\n---\n\n".join(chunks)]
    if missing:
        parts.append(_unresolved_section(missing))

    notes = read_file(os.path.join(steps_dir(proof_dir), "99_notes.md")).strip()
    if notes:
        # The model owns this section when the checkpoint is complete. When it
        # is not, the generated gap list above is authoritative, so drop the
        # model's copy -- but only that section, not the Key Ideas and
        # Deviations that follow it.
        if missing:
            notes = _UNRESOLVED_SECTION_RE.sub("", notes).strip()
        if notes:
            parts.append(notes)

    return "\n\n".join(parts).rstrip() + "\n", present, missing


# ---------------------------------------------------------------------------
# Carrying an interrupted attempt forward
# ---------------------------------------------------------------------------
#
# An interrupted attempt leaves its finished steps on disk, but the next
# attempt starts in a new ``proof_N/`` directory, so without help it rewrites
# all of them. Two things fix that, and both are needed: the checkpoints are
# copied into the new attempt's directory, and the prompt is told which steps
# are already written. Copying alone would leave the model unaware it has
# anything to continue from; telling it alone would leave the assembly reading
# an empty directory.

def _copy_bytes(source: str, target: str) -> bool:
    """Copy a file verbatim, skipping an empty source and an existing target.

    Returns whether anything was copied. Byte-level copying is deliberate: see
    ``carry_forward_checkpoints`` for why the text path would corrupt the file.
    """
    try:
        if os.path.exists(target) or os.path.getsize(source) == 0:
            return False
        shutil.copyfile(source, target)
        return True
    except OSError:                     # absent or unreadable: nothing to carry
        return False


def interrupted_partial(previous_proof_dir: str) -> str:
    """Path to an interrupted attempt's partial document, or ``""``.

    ``proof_partial.md`` is written in exactly one situation: step checkpoints
    survived a session that ended before it wrote ``proof.md``. An attempt that
    *did* write ``proof.md`` failed on its mathematics instead, and its steps
    must not be inherited -- a verifier has already judged them, and carrying
    them over would freeze the same rejected argument into every later attempt.
    """
    if not previous_proof_dir:
        return ""
    if os.path.exists(os.path.join(previous_proof_dir, "proof.md")):
        return ""
    partial = os.path.join(previous_proof_dir, "proof_partial.md")
    return partial if os.path.exists(partial) else ""


def carry_forward_checkpoints(proof_dir: str, previous_proof_dir: str,
                              decomposition: dict | None):
    """Copy an interrupted attempt's checkpoints into the new attempt.

    Returns ``(carried, missing)``: the ids the new attempt inherits, and those
    it still has to write. Copying rather than pointing the assembler at the old
    directory keeps each attempt self-contained -- if this one is interrupted
    too, it still assembles from what exists inside its own directory.

    Nothing in the destination is overwritten: a resumed session may already
    have written some of these files itself.

    Checkpoints are copied byte for byte rather than through ``read_text`` /
    ``write_text``. The text round trip is not transparent: ``write_text`` opens
    the file in text mode, so a model-written ``\\r\\n`` is read back as ``\\r\\n``
    and translated again on the way out, doubling the carriage returns once per
    newline. It would also transcode a checkpoint the decoder had to fall back
    on, and re-serialise a file whose content is already exactly right.
    """
    order = step_order(decomposition, previous_proof_dir)
    if not interrupted_partial(previous_proof_dir):
        # Judged, or empty. Either way there is no continuation to carry, and
        # callers must not be able to get one by forgetting to ask.
        return [], order

    src = steps_dir(previous_proof_dir)
    dst = steps_dir(proof_dir)
    if not os.path.isdir(src):
        return [], order

    os.makedirs(dst, exist_ok=True)

    _copy_bytes(os.path.join(src, "00_front.md"), os.path.join(dst, "00_front.md"))

    carried: list[str] = []
    for step_id in order:
        target = os.path.join(dst, step_id + ".md")
        if os.path.exists(target):
            carried.append(step_id)     # already written by the session in progress
            continue
        if looks_like_step(read_file(os.path.join(src, step_id + ".md"))):
            # Only claim the step once its file is actually there: a step
            # reported as written but absent would never be written again, and
            # the assembly would then call this attempt incomplete.
            if _copy_bytes(os.path.join(src, step_id + ".md"), target):
                carried.append(step_id)

    # Notes summarise the session that died, so the model's own Unresolved list
    # describes steps this attempt may now prove. Drop just that section and
    # keep Key Ideas / Deviations, which are still true of the plan. This one
    # has to be rewritten, so normalise its line endings before writing it.
    notes = os.path.join(dst, "99_notes.md")
    if not os.path.exists(notes):
        text = _UNRESOLVED_SECTION_RE.sub("", read_file(os.path.join(src, "99_notes.md")))
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if text:
            write_file(notes, text)

    # The scratchpad is the most useful thing to inherit: it records the routes
    # already tried on the steps that are still missing. It lives beside the
    # checkpoint directory, not inside it.
    _copy_bytes(os.path.join(previous_proof_dir, "scratchpad.md"),
                os.path.join(proof_dir, "scratchpad.md"))

    return carried, [s for s in order if s not in carried]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class DecompositionLogger:
    """Logger for decomposition prover that writes structured logs."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.decomp_dir = os.path.join(output_dir, "decomposition")
        os.makedirs(self.decomp_dir, exist_ok=True)

        # Main status and timeline log (top-level, not per-attempt)
        self.status_file = os.path.join(self.decomp_dir, "STATUS.md")
        self.main_log_file = os.path.join(self.decomp_dir, "log.txt")

        # Initialize status
        self._write_status({
            "state": "STARTING",
            "attempt": 1,
            "revision": 1,
            "proof": 1,
            "last_update": datetime.now().isoformat(),
        })

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _write_status(self, status: dict) -> None:
        """Write current status to STATUS.md."""
        content = f"""# Decomposition Prover Status

**Last Updated:** {status.get('last_update', datetime.now().isoformat())}

## Current State

| Field | Value |
|-------|-------|
| State | {status.get('state', 'UNKNOWN')} |
| Attempt | {status.get('attempt', 1)} |
| Revision | {status.get('revision', 1)} |
| Proof | {status.get('proof', 1)} |

## Recent Activity

{status.get('recent_activity', '')}
"""
        write_file(self.status_file, content)

    def update_status(
        self,
        state: str = None,
        attempt: int = None,
        revision: int = None,
        proof: int = None,
        recent_activity: str = None,
    ) -> None:
        """Update the status file with current state."""
        # Read existing status
        existing = {}
        if os.path.exists(self.status_file):
            # Parse existing values (simple approach)
            existing = {
                "state": "UNKNOWN",
                "attempt": 1,
                "revision": 1,
                "proof": 1,
                "recent_activity": "",
            }

        # Update with new values
        if state is not None:
            existing["state"] = state
        if attempt is not None:
            existing["attempt"] = attempt
        if revision is not None:
            existing["revision"] = revision
        if proof is not None:
            existing["proof"] = proof
        if recent_activity is not None:
            existing["recent_activity"] = recent_activity

        existing["last_update"] = datetime.now().isoformat()
        self._write_status(existing)

    def log(self, message: str, agent: str = None) -> None:
        """Log a message to the main timeline log."""
        timestamp = self._timestamp()
        log_line = f"[{timestamp}] {message}\n"

        # Write to main log
        with open(self.main_log_file, "a", encoding="utf-8") as f:
            f.write(log_line)

        # Print to console
        print(f"[DecompProver] {message}")

    def log_agent_call(
        self,
        agent: str,
        action: str,
        model: str,
        details: dict = None,
    ) -> None:
        """Log an agent call with details."""
        details_str = ""
        if details:
            details_str = " | " + " | ".join(f"{k}={v}" for k, v in details.items())
        message = f"[{agent.upper()}] {action} (model={model}){details_str}"
        self.log(message)

    def log_agent_result(
        self,
        agent: str,
        result: str,
        elapsed: float = None,
        tokens_in: int = None,
        tokens_out: int = None,
    ) -> None:
        """Log an agent result."""
        parts = [f"[{agent.upper()}] Result: {result}"]
        if elapsed is not None:
            parts.append(f"elapsed={elapsed:.1f}s")
        if tokens_in is not None:
            parts.append(f"tokens_in={tokens_in}")
        if tokens_out is not None:
            parts.append(f"tokens_out={tokens_out}")
        self.log(" | ".join(parts))

    def save_agent_output(
        self,
        output: str,
        path: str,
    ) -> None:
        """Save raw LLM response to a structured path."""
        write_file(path, output)


def parse_regulator_decision(response: str) -> str:
    """Extract decision from regulator response.

    Returns one of: REVISE_PROOF, REVISE_PLAN, REWRITE
    """
    response_upper = response.upper()
    # Check for new 3-option decisions first
    for decision in ["REVISE_PROOF", "REVISE_PLAN", "REWRITE"]:
        if f"DECISION: {decision}" in response_upper:
            return decision
        if f"## DECISION: {decision}" in response_upper:
            return decision
    # Legacy support: map old REVISE to REVISE_PROOF
    if "DECISION: REVISE" in response_upper or "## DECISION: REVISE" in response_upper:
        return "REVISE_PROOF"
    # Default to REVISE_PROOF if unclear (cheapest retry option)
    return "REVISE_PROOF"


# ---------------------------------------------------------------------------
# Decomposition state management
# ---------------------------------------------------------------------------

class DecompositionState:
    """Tracks the state of a decomposition-based proof attempt.

    Three-level hierarchy:
    - attempt: Complete decomposition strategy (REWRITE creates new attempt)
    - revision: Version of the decomposition within an attempt (REVISE_PLAN creates new revision)
    - proof: Proof attempt for a given decomposition (REVISE_PROOF creates new proof)

    Directory structure:
        attempt_1/
          revision_1/
            decomposition.yaml
            proof_1/
              proof.md, verification reports, regulator_decision.md
            proof_2/
              ...
          revision_2/
            decomposition.yaml  (revised plan)
            proof_1/
              ...
        attempt_2/  (new strategy)
          ...
    """

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.decomp_dir = os.path.join(output_dir, "decomposition")
        self.attempt = 1
        self.revision = 1
        self.proof = 1
        self.decomposition = None
        self.attempt_history = []  # list of failure info for rewrites

    def get_attempt_dir(self) -> str:
        """Get directory for current decomposition attempt."""
        return os.path.join(self.decomp_dir, f"attempt_{self.attempt}")

    def get_revision_dir(self) -> str:
        """Get directory for current revision within attempt."""
        return os.path.join(self.get_attempt_dir(), f"revision_{self.revision}")

    def get_proof_dir(self) -> str:
        """Get directory for current proof attempt."""
        return os.path.join(self.get_revision_dir(), f"proof_{self.proof}")

    def get_plan_history_file(self) -> str:
        """Path to the cross-attempt plan history (regulator is the sole writer)."""
        return os.path.join(self.decomp_dir, "plan_history.md")

    def ensure_plan_history(self) -> None:
        """Create plan_history.md with a brief preamble if it does not exist yet.

        The file is appended to by the regulator on REVISE_PLAN / REWRITE
        decisions; the decomposer reads it before proposing a new plan.
        """
        path = self.get_plan_history_file()
        if os.path.exists(path):
            return
        os.makedirs(self.decomp_dir, exist_ok=True)
        preamble = (
            "# Plan History\n\n"
            "Curated record of abandoned / revised decomposition plans. The "
            "**regulator** is the only agent permitted to append to this file, "
            "and it does so when its decision is REVISE_PLAN or REWRITE. The "
            "**decomposer** must read this file before producing a new plan.\n\n"
            "Format: each entry is a level-2 section identifying the attempt / "
            "revision, the strategy in one sentence, the key step statements "
            "verbatim, the regulator's diagnosis of why the plan failed, what "
            "NOT to try again, and what (if anything) might still be reused.\n\n"
            "If a decomposer truly needs the full prior YAML, it can read it "
            "directly at `decomposition/attempt_*/revision_*/decomposition.yaml`.\n\n"
            "---\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(preamble)

    def save_decomposition(self, decomposition: dict) -> None:
        """Save decomposition to the current revision directory.

        Each revision has one decomposition.yaml that applies to all proofs within it.
        """
        self.decomposition = decomposition
        revision_dir = self.get_revision_dir()
        os.makedirs(revision_dir, exist_ok=True)
        path = os.path.join(revision_dir, "decomposition.yaml")
        write_file(path, yaml.dump(decomposition, default_flow_style=False))

    def load_decomposition(self) -> dict | None:
        """Load decomposition from the current revision directory."""
        path = os.path.join(self.get_revision_dir(), "decomposition.yaml")
        content = read_file(path)
        if content:
            self.decomposition = parse_decomposition(content)
            return self.decomposition
        return None

    def save_proof(self, proof: str) -> None:
        """Save proof to the current proof attempt directory."""
        proof_dir = self.get_proof_dir()
        os.makedirs(proof_dir, exist_ok=True)
        write_file(os.path.join(proof_dir, "proof.md"), proof)
        # Also save to top-level output for final result
        write_file(os.path.join(self.output_dir, "proof.md"), proof)

    def save_regulator_decision(self, decision: str, response: str) -> None:
        """Save regulator decision."""
        path = os.path.join(self.get_proof_dir(), "regulator_decision.md")
        write_file(path, response)

    def new_proof(self) -> None:
        """Start a new proof attempt with the same decomposition (REVISE_PROOF)."""
        self.proof += 1
        os.makedirs(self.get_proof_dir(), exist_ok=True)

    def new_revision(self) -> None:
        """Start a new revision of the decomposition (REVISE_PLAN)."""
        self.revision += 1
        self.proof = 1
        os.makedirs(self.get_revision_dir(), exist_ok=True)
        os.makedirs(self.get_proof_dir(), exist_ok=True)

    def new_attempt(self) -> None:
        """Start a completely new decomposition attempt (REWRITE)."""
        # Save failure history
        if self.decomposition:
            self.attempt_history.append({
                "attempt": self.attempt,
                "revisions": self.revision,
                "proofs": self.proof,
                "decomposition": self.decomposition,
            })
        # Reset for new attempt
        self.attempt += 1
        self.revision = 1
        self.proof = 1
        self.decomposition = None
        os.makedirs(self.get_attempt_dir(), exist_ok=True)
        os.makedirs(self.get_revision_dir(), exist_ok=True)
        os.makedirs(self.get_proof_dir(), exist_ok=True)

    def get_failure_history(self) -> str:
        """Get formatted failure history for rewrite mode."""
        if not self.attempt_history:
            return "No previous attempts."

        lines = ["# Previous Attempt Failures\n"]
        for hist in self.attempt_history:
            lines.append(f"## Attempt {hist['attempt']}\n")
            lines.append(f"Revisions tried: {hist['revisions']}\n")
            lines.append(f"Proofs in last revision: {hist['proofs']}\n")
            lines.append("\n")
        return "".join(lines)

    def get_revision_summary(self) -> str:
        """Get summary of proof attempts in current revision for regulator."""
        lines = []
        for i in range(1, self.proof):
            proof_dir = os.path.join(self.get_revision_dir(), f"proof_{i}")
            reg_decision = read_file(os.path.join(proof_dir, "regulator_decision.md"))
            if reg_decision:
                lines.append(f"## Proof Attempt {i}\n")
                lines.append(f"Regulator decision:\n{reg_decision[:500]}...\n\n")
        return "".join(lines) if lines else "This is the first proof attempt for this revision."

    def get_full_attempt_history(self) -> str:
        """Get comprehensive history of all attempts for FINAL mode failure analysis."""
        lines = ["# Complete Attempt History\n\n"]

        # Include saved history from previous attempts
        for hist in self.attempt_history:
            lines.append(f"## Attempt {hist['attempt']}\n")
            lines.append(f"- Total revisions: {hist['revisions']}\n")
            lines.append(f"- Proofs in last revision: {hist['proofs']}\n\n")

        # Include current attempt details
        lines.append(f"## Attempt {self.attempt} (current)\n")
        lines.append(f"- Total revisions: {self.revision}\n")
        lines.append(f"- Proofs in current revision: {self.proof}\n\n")

        # Scan all proof directories for verification reports and decisions
        lines.append("### Detailed Proof History\n\n")
        for a in range(1, self.attempt + 1):
            attempt_dir = os.path.join(self.decomp_dir, f"attempt_{a}")
            if not os.path.isdir(attempt_dir):
                continue
            max_rev = self.revision if a == self.attempt else _find_max_numbered_dir(attempt_dir, "revision_")
            for r in range(1, max_rev + 1):
                revision_dir = os.path.join(attempt_dir, f"revision_{r}")
                if not os.path.isdir(revision_dir):
                    continue
                max_proof = self.proof if (a == self.attempt and r == self.revision) else _find_max_numbered_dir(revision_dir, "proof_")
                for p in range(1, max_proof + 1):
                    proof_dir = os.path.join(revision_dir, f"proof_{p}")
                    lines.append(f"#### Attempt {a}, Revision {r}, Proof {p}\n")

                    # Check verification results
                    structural = read_file(os.path.join(proof_dir, "structural_verification.md"))
                    detailed = read_file(os.path.join(proof_dir, "detailed_verification.md"))
                    decision = read_file(os.path.join(proof_dir, "regulator_decision.md"))

                    if structural:
                        # Extract verdict summary (first few lines)
                        verdict_lines = structural.split('\n')[:5]
                        lines.append(f"Structural verification:\n```\n{chr(10).join(verdict_lines)}...\n```\n")
                    if detailed:
                        verdict_lines = detailed.split('\n')[:5]
                        lines.append(f"Detailed verification:\n```\n{chr(10).join(verdict_lines)}...\n```\n")
                    if decision:
                        lines.append(f"Regulator decision:\n```\n{decision[:300]}...\n```\n")
                    lines.append("\n")

        return "".join(lines)


# ---------------------------------------------------------------------------
# Resume detection
# ---------------------------------------------------------------------------

def _file_nonempty(path: str) -> bool:
    """Check if a file exists and is non-empty."""
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _find_max_numbered_dir(parent_dir: str, prefix: str) -> int:
    """Find the highest numbered subdirectory with given prefix."""
    if not os.path.isdir(parent_dir):
        return 0
    nums = []
    for name in os.listdir(parent_dir):
        if name.startswith(prefix):
            try:
                nums.append(int(name.split("_", 1)[1]))
            except (ValueError, IndexError):
                continue
    return max(nums) if nums else 0


def detect_decomposition_resume(output_dir: str) -> dict:
    """Scan the output directory for decomposition progress from a previous run.

    Three-level hierarchy:
    - attempt_N/           (REWRITE creates new attempt)
      - revision_M/        (REVISE_PLAN creates new revision)
        - decomposition.yaml
        - proof_K/         (REVISE_PROOF creates new proof)
          - proof.md
          - structural_verification.md
          - detailed_verification.md
          - regulator_decision.md

    Returns a dict with resume information:
        {
            "has_progress": bool,
            "attempt": int,
            "revision": int,
            "proof": int,
            "decomposition": dict|None,
            "resume_point": str,        # "fresh", "decompose", "prove",
                                        # "verify_structural", "verify_detailed",
                                        # "regulator", "done"
            "attempt_history": list,
        }
    """
    decomp_dir = os.path.join(output_dir, "decomposition")
    result = {
        "has_progress": False,
        "attempt": 1,
        "revision": 1,
        "proof": 1,
        "decomposition": None,
        "resume_point": "fresh",
        "attempt_history": [],
    }

    if not os.path.isdir(decomp_dir):
        return result

    # Find the highest attempt directory
    latest_attempt = _find_max_numbered_dir(decomp_dir, "attempt_")
    if latest_attempt == 0:
        return result

    # Build attempt history from previous (failed) attempts
    attempt_history = []
    for att_num in range(1, latest_attempt):
        att_dir = os.path.join(decomp_dir, f"attempt_{att_num}")
        last_rev = _find_max_numbered_dir(att_dir, "revision_")
        if last_rev > 0:
            rev_dir = os.path.join(att_dir, f"revision_{last_rev}")
            decomp_path = os.path.join(rev_dir, "decomposition.yaml")
            decomp_content = read_file(decomp_path)
            if decomp_content:
                last_proof = _find_max_numbered_dir(rev_dir, "proof_")
                try:
                    decomp = parse_decomposition(decomp_content)
                except yaml.YAMLError:
                    decomp = None  # Skip invalid YAML in history
                attempt_history.append({
                    "attempt": att_num,
                    "revisions": last_rev,
                    "proofs": last_proof,
                    "decomposition": decomp,
                })

    result["attempt_history"] = attempt_history
    result["attempt"] = latest_attempt

    # Analyze the latest attempt
    att_dir = os.path.join(decomp_dir, f"attempt_{latest_attempt}")
    latest_revision = _find_max_numbered_dir(att_dir, "revision_")

    if latest_revision == 0:
        # No revision directories yet - need to start fresh decomposition
        result["has_progress"] = len(attempt_history) > 0
        result["resume_point"] = "decompose"
        return result

    result["revision"] = latest_revision
    rev_dir = os.path.join(att_dir, f"revision_{latest_revision}")

    # Check if decomposition exists in this revision
    decomp_path = os.path.join(rev_dir, "decomposition.yaml")
    decomp_content = read_file(decomp_path)
    if not decomp_content:
        # Revision directory exists but no decomposition - need decomposition
        result["has_progress"] = len(attempt_history) > 0 or latest_revision > 1
        # If revision > 1, we're in the middle of a REVISE operation
        # If revision == 1, we're starting fresh (CREATE or REWRITE)
        if latest_revision > 1:
            result["resume_point"] = "decompose_revise"
        else:
            result["resume_point"] = "decompose"
        return result

    try:
        decomposition = parse_decomposition(decomp_content)
    except yaml.YAMLError:
        # Invalid YAML in current decomposition - need to re-decompose
        result["has_progress"] = len(attempt_history) > 0 or latest_revision > 1
        result["resume_point"] = "decompose" if latest_revision == 1 else "decompose_revise"
        return result
    result["decomposition"] = decomposition
    result["has_progress"] = True

    # Find the latest proof attempt
    latest_proof = _find_max_numbered_dir(rev_dir, "proof_")
    if latest_proof == 0:
        # No proof directories yet - need to start proving
        result["resume_point"] = "prove"
        return result

    result["proof"] = latest_proof
    proof_dir = os.path.join(rev_dir, f"proof_{latest_proof}")

    # Check verification status
    detailed_path = os.path.join(proof_dir, "detailed_verification.md")
    structural_path = os.path.join(proof_dir, "structural_verification.md")
    proof_path = os.path.join(proof_dir, "proof.md")
    regulator_path = os.path.join(proof_dir, "regulator_decision.md")

    # Check if detailed verification exists and passed
    if _file_nonempty(detailed_path):
        if parse_report_verdict(detailed_path) == "PASS":
            result["resume_point"] = "done"
            return result
        # Detailed failed - check if regulator was called
        if _file_nonempty(regulator_path):
            # Regulator was called, need to start new proof attempt
            result["proof"] = latest_proof + 1
            result["resume_point"] = "prove"
            return result
        # No regulator decision yet - need to call regulator
        result["resume_point"] = "regulator"
        return result

    # Check if structural verification exists
    if _file_nonempty(structural_path):
        if parse_report_verdict(structural_path) == "PASS":
            # Structural passed, need detailed
            result["resume_point"] = "verify_detailed"
            return result
        # Structural failed - check if regulator was called
        if _file_nonempty(regulator_path):
            result["proof"] = latest_proof + 1
            result["resume_point"] = "prove"
            return result
        result["resume_point"] = "regulator"
        return result

    # Check if proof exists
    if _file_nonempty(proof_path):
        result["resume_point"] = "verify_structural"
        return result

    # Have decomposition but no proof yet
    result["resume_point"] = "prove"
    return result


# ---------------------------------------------------------------------------
# Helper to get model for an agent
# ---------------------------------------------------------------------------

def get_agent_role_cfg(config: dict, agent_name: str) -> dict:
    """Return the per-agent role config dict for *agent_name*.

    Shape: ``{provider: 'codex'|'claude'|'gemini', model?: '...', ...knobs...}``.
    The chosen provider's global section is overlaid with these overrides at
    call time (see :func:`model_runner.resolve_agent_provider_config`).
    """
    decomp_config = config.get("decomposition", {})
    models = decomp_config.get("models", DEFAULT_MODELS)
    role_cfg = models.get(agent_name, DEFAULT_MODELS.get(agent_name, {"provider": "claude"}))
    if not isinstance(role_cfg, dict) or "provider" not in role_cfg:
        raise ValueError(
            f"decomposition.models.{agent_name} must be a dict with a 'provider' key "
            f"(e.g. {{provider: 'codex', model: 'gpt-5.5'}}). Got: {role_cfg!r}"
        )
    return role_cfg


def get_agent_provider(role_cfg: dict) -> str:
    """Convenience: extract the provider string from a role config dict."""
    return role_cfg["provider"]


def get_claude_opts_for_role(config: dict, role_cfg: dict) -> dict:
    """Build claude_opts for a per-agent role config when the provider is Claude.

    Returns the standard claude_opts dict ({cli_path, model, env}). The
    agent-level ``model`` override (if any) takes precedence over the global
    ``claude.{subscription|api_key|bedrock}.model`` value.

    For non-Claude providers, returns an empty dict — the codex/gemini runners
    read their config directly from the (already-merged) provider section.
    """
    if role_cfg.get("provider") != "claude":
        return {}
    claude_cfg = config.get("claude", {})
    auth_mode = claude_cfg.get("provider", "subscription")
    model_override = role_cfg.get("model")
    if auth_mode == "subscription":
        model = model_override or claude_cfg.get("subscription", {}).get("model", "claude-fable-5-1")
        env = {}
    elif auth_mode == "bedrock":
        bedrock = claude_cfg.get("bedrock", {})
        model = model_override or bedrock.get("model", "anthropic.claude-fable-5-1")
        env = {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_PROFILE": bedrock.get("aws_profile", "default"),
        }
    elif auth_mode == "api_key":
        api_cfg = claude_cfg.get("api_key", {})
        model = model_override or api_cfg.get("model", "claude-fable-5-1")
        env = {"ANTHROPIC_API_KEY": api_cfg.get("key", "")}
    else:
        raise ValueError(
            f"config.yaml: unknown claude.provider {auth_mode!r}. "
            f"Use 'subscription', 'bedrock', or 'api_key'."
        )
    return {
        "cli_path": claude_cfg.get("cli_path", "claude"),
        "model": model,
        "env": env,
    }


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------

async def run_decomposer(
    state: DecompositionState,
    problem_file: str,
    related_work_file: str,
    prompts_dir: str,
    config: dict,
    claude_opts: dict,
    mode: str = "CREATE",
    verification_feedback: str = "",
    regulator_guidance: str = "",
    decomp_logger: DecompositionLogger = None,
    tracker=None,
) -> dict:
    """Run the decomposer agent to create/revise/rewrite a decomposition.

    Args:
        mode: "CREATE" for fresh decomposition, "REVISE" to modify plan based on
              verification feedback, "REWRITE" for completely new strategy.
        verification_feedback: Combined verification report (for REVISE mode).
        regulator_guidance: Regulator's suggestions for the decomposer (for REVISE/REWRITE).
    """

    role_cfg = get_agent_role_cfg(config, "decomposer")
    model_provider = role_cfg["provider"]

    # Resolve previous-revision paths for REVISE mode. In CREATE / REWRITE
    # these are empty — the decomposer prompt's Input Files section renders
    # those slots as empty strings.
    prev_proof_file = ""
    if mode == "REVISE":
        prev_rev_num = state.revision - 1
        prev_rev_dir = os.path.join(state.get_attempt_dir(), f"revision_{prev_rev_num}")
        last_proof = _find_max_numbered_dir(prev_rev_dir, "proof_")
        prev_proof_file = (
            os.path.join(prev_rev_dir, f"proof_{last_proof}", "proof.md")
            if last_proof > 0 else ""
        )

    # Resolve human help file from output_dir (run.sh copies the global files
    # there, and the UI edits them in-place).
    human_help_file = os.path.join(state.output_dir, "human_help", "additional_prove_human_help_global.md")

    # Ensure revision directory exists for output
    revision_dir = state.get_revision_dir()
    os.makedirs(revision_dir, exist_ok=True)
    decomposition_output_file = os.path.join(revision_dir, "decomposition.yaml")

    # For REVISE mode, point to previous revision's decomposition
    prev_rev_num = state.revision - 1 if mode == "REVISE" else state.revision
    prev_decomp_file = os.path.join(state.get_attempt_dir(), f"revision_{prev_rev_num}", "decomposition.yaml")

    prompt = load_prompt(
        prompts_dir,
        "decomposition-prover/decomposition.md",
        mode=mode,
        problem_file=problem_file,
        related_work_file=related_work_file,
        problem_id=os.path.basename(problem_file),
        attempt_number=state.attempt,
        revision_number=state.revision,
        timestamp=datetime.now().isoformat(),
        output_file=decomposition_output_file,
        current_decomposition_file=prev_decomp_file if mode == "REVISE" else "",
        verification_feedback=verification_feedback if mode == "REVISE" else "",
        regulator_guidance=regulator_guidance if mode in ("REVISE", "REWRITE") else "",
        previous_proof_file=prev_proof_file if mode == "REVISE" else "",
        human_help_file=human_help_file,
        plan_history_file=state.get_plan_history_file(),
    )

    if decomp_logger:
        decomp_logger.log_agent_call(
            "decomposer", f"{mode} mode",
            model_provider,
            {"attempt": state.attempt, "revision": state.revision, "proof": state.proof}
        )
        decomp_logger.update_status(
            state="DECOMPOSING",
            attempt=state.attempt,
            revision=state.revision,
            recent_activity=f"Running decomposer in {mode} mode"
        )

    # Run the model
    response = await run_model_for_agent(
        agent_role_cfg=role_cfg,
        prompt=prompt,
        working_dir=state.output_dir,
        config=config,
        claude_opts=get_claude_opts_for_role(config, role_cfg) if model_provider == "claude" else claude_opts,
        tracker=tracker,
        call_name=f"decomposer_{mode.lower()}",
    )

    # Save LLM response into structured path (in revision directory)
    if decomp_logger:
        resp_path = os.path.join(revision_dir, "decomposer_response.md")
        decomp_logger.save_agent_output(response, resp_path)
        decomp_logger.log_agent_result("decomposer", f"{mode} completed")

    # Read the file the agent wrote via tool call
    try:
        decomposition = state.load_decomposition()
        if decomposition:
            return decomposition
    except yaml.YAMLError as e:
        # Agent wrote invalid YAML to file - will try parsing response instead
        if decomp_logger:
            decomp_logger.log(f"Warning: Decomposition file has invalid YAML: {e}")

    # Agent failed to write the file or wrote invalid YAML — fall back to parsing response text
    try:
        decomposition = parse_decomposition(response)
    except (yaml.YAMLError, AttributeError) as e:
        raise RuntimeError(
            f"Decomposer did not produce valid YAML and did not write to {decomposition_output_file}.\n"
            f"Parse error: {e}\nResponse preview: {response[:500]}"
        ) from e

    state.save_decomposition(decomposition)
    return decomposition


async def run_single_prover(
    state: DecompositionState,
    problem_file: str,
    related_work_file: str,
    prompts_dir: str,
    config: dict,
    claude_opts: dict,
    decomp_logger: DecompositionLogger = None,
    tracker=None,
) -> str:
    """Run the single prover agent to write a complete proof following the decomposition plan."""

    role_cfg = get_agent_role_cfg(config, "single_prover")
    model_provider = role_cfg["provider"]

    # Resolve human help file from output_dir (run.sh copies the global files
    # there, and the UI edits them in-place).
    human_help_file = os.path.join(state.output_dir, "human_help", "additional_prove_human_help_global.md")

    # Build the output path
    proof_dir = state.get_proof_dir()
    os.makedirs(proof_dir, exist_ok=True)
    output_file = os.path.join(proof_dir, "proof.md")

    # Decomposition is in the revision directory
    decomposition_file = os.path.join(state.get_revision_dir(), "decomposition.yaml")

    # Previous proof context: only for REVISE_PROOF (same revision, proof > 1)
    # For new revision or new attempt, these are empty
    previous_proof_file = ""
    previous_verification_file = ""
    previous_partial_file = ""
    carried_steps: list[str] = []
    remaining_steps: list[str] = []
    if state.proof > 1:
        prev_proof_dir = os.path.join(state.get_revision_dir(), f"proof_{state.proof - 1}")
        previous_proof_file = os.path.join(prev_proof_dir, "proof.md")
        # Use structural if detailed doesn't exist, otherwise use detailed
        detailed_path = os.path.join(prev_proof_dir, "detailed_verification.md")
        structural_path = os.path.join(prev_proof_dir, "structural_verification.md")
        if os.path.exists(detailed_path):
            previous_verification_file = detailed_path
        elif os.path.exists(structural_path):
            previous_verification_file = structural_path

        # If the previous attempt was cut off rather than judged, continue it:
        # the finished steps move into this attempt and the prompt says which
        # ones are left.
        previous_partial_file = interrupted_partial(prev_proof_dir)
        if previous_partial_file:
            carried_steps, remaining_steps = carry_forward_checkpoints(
                proof_dir, prev_proof_dir, state.decomposition)
            if decomp_logger:
                decomp_logger.log(
                    f"Continuing an interrupted attempt: carried "
                    f"{len(carried_steps)} step(s) forward "
                    f"({', '.join(carried_steps) or 'none'}); "
                    f"still to write {', '.join(remaining_steps) or 'none'}"
                )

    scratchpad_file = os.path.join(proof_dir, "scratchpad.md")
    proof_steps_dir = steps_dir(proof_dir)

    prompt = load_prompt(
        prompts_dir,
        "decomposition-prover/single_prover.md",
        problem_file=problem_file,
        related_work_file=related_work_file,
        decomposition_file=decomposition_file,
        human_help_file=human_help_file,
        previous_proof_file=previous_proof_file,
        previous_verification_file=previous_verification_file,
        previous_partial_file=previous_partial_file,
        carried_steps=", ".join(carried_steps) or "none",
        # Not a continuation, so "none to write" would read as "nothing to do":
        # every id in proof_order is the model's to write.
        remaining_steps=", ".join(remaining_steps)
                        or ("none" if previous_partial_file else "all of proof_order"),
        output_file=output_file,
        output_dir=state.output_dir,
        scratchpad_file=scratchpad_file,
        proof_steps_dir=proof_steps_dir,
    )

    if decomp_logger:
        decomp_logger.log_agent_call(
            "single_prover", "Writing complete proof",
            model_provider,
            {"attempt": state.attempt, "revision": state.revision, "proof": state.proof}
        )
        decomp_logger.update_status(
            state="PROVING",
            proof=state.proof,
            recent_activity=f"Writing proof (attempt {state.attempt}, revision {state.revision}, proof {state.proof})"
        )

    response = await run_model_for_agent(
        agent_role_cfg=role_cfg,
        prompt=prompt,
        working_dir=state.output_dir,
        config=config,
        claude_opts=get_claude_opts_for_role(config, role_cfg) if model_provider == "claude" else claude_opts,
        tracker=tracker,
        call_name=f"single_prover_a{state.attempt}_r{state.revision}_p{state.proof}",
    )

    # Save LLM response into structured path
    if decomp_logger:
        resp_path = os.path.join(proof_dir, "prover_response.md")
        decomp_logger.save_agent_output(response, resp_path)

    # Read the file the agent wrote via tool call
    content = read_file(output_file)
    if not content:
        # No proof.md. Two very different situations look like this and must not
        # be conflated:
        #
        #   1. The session ended mid-proof. The step checkpoints show how far it
        #      got, and whatever it did write is recovered below.
        #   2. The session ended before any step was written, and the last
        #      message is narration of the next action ("Let me write the proof
        #      next"). Promoting that to proof.md spends an entire attempt on
        #      something that cannot pass verification, and save_proof() would
        #      also mirror it into the run's top-level proof.md, leaving
        #      narration standing in as the run's final result.
        decomposition = state.decomposition
        if decomposition is None:
            try:
                decomposition = state.load_decomposition()
            except Exception:       # unreadable or invalid YAML
                decomposition = None

        assembled = assemble_proof_from_steps(proof_dir, problem_file, decomposition)

        if assembled:
            text, present, missing = assembled
            if missing:
                partial_file = os.path.join(proof_dir, "proof_partial.md")
                write_file(partial_file, text)
                if decomp_logger:
                    decomp_logger.log_agent_result(
                        "single_prover",
                        f"Interrupted after {len(present)} of "
                        f"{len(present) + len(missing)} steps (missing "
                        f"{', '.join(missing)}); kept as "
                        f"{os.path.basename(partial_file)}",
                    )
                raise ProofIncomplete(
                    f"The prover wrote no proof.md and its step checkpoints "
                    f"cover {len(present)} of {len(present) + len(missing)} "
                    f"steps: written {', '.join(present)}; missing "
                    f"{', '.join(missing)}. The written steps were assembled "
                    f"into {os.path.basename(partial_file)}.",
                    present, missing, partial_file,
                )
            content = text
            if decomp_logger:
                decomp_logger.log_agent_result(
                    "single_prover",
                    f"proof.md absent; assembled it from {len(present)} step "
                    "checkpoints",
                )
        elif looks_like_proof(response):
            proof_start = response.find("# Proof")
            content = response[proof_start:] if proof_start >= 0 else response
        else:
            write_file(
                os.path.join(proof_dir, "prover_rejected.md"), response or ""
            )
            if decomp_logger:
                decomp_logger.log_agent_result(
                    "single_prover",
                    "No proof produced: the agent wrote no file, left no step "
                    f"checkpoints, and its response ({len(response or '')} "
                    "chars) is not a proof",
                )
            raise ProofNotProduced(
                f"The prover wrote no {os.path.basename(output_file)}, left no "
                f"usable step checkpoints, and its response "
                f"({len(response or '')} chars) contains no proof structure. "
                "Its response was:\n\n" + (response or "").strip()[:600]
            )

    if decomp_logger:
        decomp_logger.log_agent_result("single_prover", "Proof written")

    # Save the proof
    state.save_proof(content)
    return content


async def run_regulator(
    state: DecompositionState,
    verification_report: str,
    proof_file: str,
    config: dict,
    prompts_dir: str,
    claude_opts: dict,
    decomp_logger: DecompositionLogger = None,
    tracker=None,
    mode: str = "DECIDE",
    verification_phase: str = "detailed",
) -> str:
    """Run the regulator agent to decide next action after verification failure.

    Args:
        mode: "DECIDE" for normal decision, "FINAL" for failure analysis when all limits exhausted.
        verification_phase: Which verifier produced the report this call is
            reacting to — "structural" (Phases 1–5; detailed verification did
            not run), "detailed" (Phase 6; structural passed but detailed
            failed), or "both" / "final" (FINAL mode and similar). This is
            surfaced to the regulator prompt as a hint, not a constraint.

    Returns:
        In DECIDE mode: one of REVISE_PROOF, REVISE_PLAN, REWRITE
        In FINAL mode: "FAILED" (failure analysis is written to file)
    """

    role_cfg = get_agent_role_cfg(config, "regulator")
    model_provider = role_cfg["provider"]
    decomp_config = config.get("decomposition", DEFAULT_CONFIG)

    # Build state file content
    state_content = f"""
attempt: {state.attempt}
revision: {state.revision}
proof: {state.proof}
max_proof_attempts: {decomp_config.get('max_proof_attempts', 3)}
max_revisions: {decomp_config.get('max_revisions', 2)}
max_decompositions: {decomp_config.get('max_decompositions', 3)}
"""

    # Get attempt history (all attempts for FINAL mode, current revision for DECIDE mode)
    if mode == "FINAL":
        attempt_history = state.get_full_attempt_history()
        output_file = os.path.join(state.decomp_dir, "failure_analysis.md")
    else:
        attempt_history = state.get_revision_summary()
        output_file = os.path.join(state.get_proof_dir(), "regulator_decision.md")

    prompt = load_prompt(
        prompts_dir,
        "decomposition-prover/regulator.md",
        mode=mode,
        state_file=state_content,
        decomposition_file=os.path.join(state.get_revision_dir(), "decomposition.yaml"),
        proof_file=proof_file,
        verification_report=verification_report,
        attempt_history=attempt_history,
        max_proof_attempts=decomp_config.get('max_proof_attempts', 3),
        max_revisions=decomp_config.get('max_revisions', 2),
        max_decompositions=decomp_config.get('max_decompositions', 3),
        output_file=output_file,
        plan_history_file=state.get_plan_history_file(),
        verification_phase=verification_phase,
    )

    if decomp_logger:
        if mode == "FINAL":
            decomp_logger.log_agent_call(
                "regulator", "Writing failure analysis",
                model_provider,
                {"mode": "FINAL", "attempt": state.attempt, "revision": state.revision, "proof": state.proof}
            )
            decomp_logger.update_status(
                state="ANALYZING_FAILURE",
                recent_activity="Regulator writing failure analysis (all limits exhausted)"
            )
        else:
            decomp_logger.log_agent_call(
                "regulator", "Evaluating verification failure",
                model_provider,
                {"attempt": state.attempt, "revision": state.revision, "proof": state.proof}
            )
            decomp_logger.update_status(
                state="REGULATING",
                recent_activity=f"Regulator evaluating after proof {state.proof} failed verification"
            )

    response = await run_model_for_agent(
        agent_role_cfg=role_cfg,
        prompt=prompt,
        working_dir=state.output_dir,
        config=config,
        claude_opts=get_claude_opts_for_role(config, role_cfg) if model_provider == "claude" else claude_opts,
        tracker=tracker,
        call_name=f"regulator_{mode.lower()}_a{state.attempt}_r{state.revision}_p{state.proof}",
    )

    # Check if the agent wrote the file via tool call
    content = read_file(output_file)
    if not content:
        content = response
        # Save content to expected location
        write_file(output_file, content)

    if mode == "FINAL":
        if decomp_logger:
            decomp_logger.log_agent_result("regulator", "Failure analysis complete")
        return "FAILED"

    decision = parse_regulator_decision(content)
    state.save_regulator_decision(decision, content)

    if decomp_logger:
        decomp_logger.log_agent_result("regulator", f"Decision: {decision}")

    return decision


# ---------------------------------------------------------------------------
# Proof verification
# ---------------------------------------------------------------------------

async def run_structural_verification(
    state: DecompositionState,
    problem_file: str,
    proof_file: str,
    prompts_dir: str,
    config: dict,
    claude_opts: dict,
    decomp_logger: DecompositionLogger = None,
    tracker=None,
) -> str:
    """Run structural verification on the proof.

    Returns the path to the verification report file.
    The verdict is determined separately by run_verdict().
    """
    role_cfg = get_agent_role_cfg(config, "structural_verifier")
    model_provider = role_cfg["provider"]

    # Use proof directory for verification outputs
    proof_dir = state.get_proof_dir()
    os.makedirs(proof_dir, exist_ok=True)

    output_file = os.path.join(proof_dir, "structural_verification.md")
    error_file = os.path.join(proof_dir, "error_structural_verification.md")
    decomposition_file = os.path.join(state.get_revision_dir(), "decomposition.yaml")

    # Global verification rules from output_dir (run.sh copies the global files
    # there, and the UI edits them in-place).
    additional_verify_rule_global_file = os.path.join(
        state.output_dir, "human_help", "additional_verify_rule_global.md"
    )

    prompt = load_prompt(
        prompts_dir,
        "decomposition-prover/proof_verify_structural.md",
        problem_file=problem_file,
        proof_file=proof_file,
        decomposition_file=decomposition_file,
        output_file=output_file,
        error_file=error_file,
        output_dir=state.output_dir,
        additional_verify_rule_global_file=additional_verify_rule_global_file,
    )

    if decomp_logger:
        decomp_logger.log_agent_call(
            "structural_verifier", "Structural verification of aggregated proof",
            model_provider,
            {}
        )
        decomp_logger.update_status(
            state="VERIFYING_PROOF_STRUCTURAL",
            recent_activity="Running structural verification on aggregated proof"
        )

    response = await run_model_for_agent(
        agent_role_cfg=role_cfg,
        prompt=prompt,
        working_dir=state.output_dir,
        config=config,
        claude_opts=get_claude_opts_for_role(config, role_cfg) if model_provider == "claude" else claude_opts,
        tracker=tracker,
        call_name="proof_verify_structural",
    )

    # Fall back to the agent's returned message if it did not write the file via tool call.
    if not read_file(output_file):
        write_file(output_file, response)

    if decomp_logger:
        decomp_logger.log_agent_result("structural_verifier", "Structural verification report generated")

    return output_file


async def run_detailed_verification(
    state: DecompositionState,
    problem_file: str,
    proof_file: str,
    structural_report_file: str,
    prompts_dir: str,
    config: dict,
    claude_opts: dict,
    decomp_logger: DecompositionLogger = None,
    tracker=None,
) -> str:
    """Run detailed verification on the proof.

    Returns the path to the verification report file.
    The verdict is determined separately by run_verdict().
    """
    role_cfg = get_agent_role_cfg(config, "detailed_verifier")
    model_provider = role_cfg["provider"]

    # Use proof directory for verification outputs
    proof_dir = state.get_proof_dir()
    os.makedirs(proof_dir, exist_ok=True)

    output_file = os.path.join(proof_dir, "detailed_verification.md")
    error_file = os.path.join(proof_dir, "error_detailed_verification.md")
    decomposition_file = os.path.join(state.get_revision_dir(), "decomposition.yaml")

    prompt = load_prompt(
        prompts_dir,
        "decomposition-prover/proof_verify_detailed.md",
        problem_file=problem_file,
        proof_file=proof_file,
        structural_report_file=structural_report_file,
        decomposition_file=decomposition_file,
        output_file=output_file,
        error_file=error_file,
        output_dir=state.output_dir,
    )

    if decomp_logger:
        decomp_logger.log_agent_call(
            "detailed_verifier", "Detailed verification of aggregated proof",
            model_provider,
            {}
        )
        decomp_logger.update_status(
            state="VERIFYING_PROOF_DETAILED",
            recent_activity="Running detailed verification on aggregated proof"
        )

    response = await run_model_for_agent(
        agent_role_cfg=role_cfg,
        prompt=prompt,
        working_dir=state.output_dir,
        config=config,
        claude_opts=get_claude_opts_for_role(config, role_cfg) if model_provider == "claude" else claude_opts,
        tracker=tracker,
        call_name="proof_verify_detailed",
    )

    # Fall back to the agent's returned message if it did not write the file via tool call.
    if not read_file(output_file):
        write_file(output_file, response)

    if decomp_logger:
        decomp_logger.log_agent_result("detailed_verifier", "Detailed verification report generated")

    return output_file


# ---------------------------------------------------------------------------
# Verification verdict — deterministic, code-only
# ---------------------------------------------------------------------------
#
# The verdict used to be delegated to a model (prompt now archived at
# prompts/decomposition-prover/archive/verdict_proof.md). That task reduces to
# reading one well-defined field per report and comparing it to PASS, so it is
# computed in code instead. This removes a per-attempt model round-trip and,
# more importantly, removes the possibility of a model accepting a report that
# does not say PASS.

_VERDICT_RE = re.compile(
    r"overall\s+(?:structural\s+)?verdict\s*:?\s*\**\s*(PASS|FAIL)"
    r"(?!\s*/\s*(?:PASS|FAIL)\s*\])",   # never accept an un-filled [PASS/FAIL] slot
    re.IGNORECASE,
)


def parse_report_verdict(path: str) -> str:
    """Extract the verdict from a verification report.

    Returns one of:
        "PASS"        — report explicitly says Overall Verdict: PASS
        "FAIL"        — report explicitly says Overall Verdict: FAIL
        "MISSING"     — path empty, file absent, or file blank
        "UNPARSEABLE" — no verdict field found

    The verdict field is emitted in the trailing Summary block, so the LAST
    occurrence is authoritative; earlier hits may be quoted prose (e.g. a
    detailed report citing the structural report's conclusion).
    """
    if not path:
        return "MISSING"
    text = read_file(path)          # compat.read_text returns "" when absent
    if not text or not text.strip():
        return "MISSING"
    matches = _VERDICT_RE.findall(text)
    if not matches:
        return "UNPARSEABLE"
    return matches[-1].upper()


def compute_verdict(mode: str, structural_report: str,
                    detailed_report: str = "") -> str:
    """Return "DONE" only when every required report explicitly says PASS.

    Mirrors the rules the old prompt stated: any missing, blank, unparseable or
    failed report yields "CONTINUE"; FINAL mode additionally requires the
    detailed report.
    """
    if parse_report_verdict(structural_report) != "PASS":
        return "CONTINUE"
    if mode.upper() == "FINAL" and parse_report_verdict(detailed_report) != "PASS":
        return "CONTINUE"
    return "DONE"


async def run_verdict(
    state: DecompositionState,
    structural_report_file: str,
    prompts_dir: str,
    config: dict,
    claude_opts: dict,
    mode: str = "FINAL",
    detailed_report_file: str = "",
    decomp_logger: DecompositionLogger = None,
    tracker=None,
) -> str:
    """Compute the verdict on verification reports.

    Kept ``async`` and with its original signature so existing call sites need
    no change. ``prompts_dir`` / ``config`` / ``claude_opts`` / ``tracker`` are
    no longer consumed — they are retained for compatibility only.

    Args:
        mode: "STRUCTURAL" to check structural only, "FINAL" to check both
        structural_report_file: Path to structural verification report
        detailed_report_file: Path to detailed verification report (FINAL only)

    Returns "DONE" if verification passes, "CONTINUE" otherwise.
    """
    state_name = "VERDICT_STRUCTURAL" if mode == "STRUCTURAL" else "VERDICT_FINAL"
    if decomp_logger:
        decomp_logger.log(f"Verdict ({mode}): reading report files directly")
        decomp_logger.update_status(
            state=state_name,
            recent_activity=f"Computing {mode.lower()} verdict from report files",
        )

    structural_parsed = parse_report_verdict(structural_report_file)
    verdict = compute_verdict(mode, structural_report_file, detailed_report_file)

    if decomp_logger:
        detail = f"structural={structural_parsed}"
        if mode.upper() == "FINAL":
            detail += f", detailed={parse_report_verdict(detailed_report_file)}"
        decomp_logger.log(f"Verdict ({mode}) evidence: {detail} -> {verdict}")
        decomp_logger.log_agent_result("verdict", f"{mode} verdict: {verdict}")

    return verdict


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def run_decomposition_prover(
    problem_file: str,
    related_work_file: str,
    output_dir: str,
    config: dict,
    prompts_dir: str,
    claude_opts: dict,
    logger=None,
    tracker=None,
) -> str:
    """Main entry point for decomposition-based proving.

    Simplified pipeline:
    1. Decomposer creates a proof plan (decomposition)
    2. Single prover writes a complete proof following the plan
    3. Structural verification (Phases 1-5)
    4. Detailed verification (Phase 6)
    5. If verification fails, regulator decides: REVISE_PROOF, REVISE_PLAN, or REWRITE

    Returns the content of the final proof.md.
    """
    decomp_config = config.get("decomposition", DEFAULT_CONFIG)
    max_decompositions = decomp_config.get("max_decompositions", 3)
    max_revisions = decomp_config.get("max_revisions", 2)
    max_proof_attempts = decomp_config.get("max_proof_attempts", 3)

    # Initialize state and logger
    state = DecompositionState(output_dir)
    decomp_logger = DecompositionLogger(output_dir)

    # Log model configuration
    models = decomp_config.get("models", DEFAULT_MODELS)
    decomp_logger.log(f"Model configuration: {models}")

    # --- Resume detection ---
    resume_info = detect_decomposition_resume(output_dir)
    resume_point = resume_info["resume_point"]

    # Handle already-done case
    if resume_point == "done":
        decomp_logger.log("RESUMING: Proof already verified successfully, nothing to do.")
        proof_file = os.path.join(output_dir, "proof.md")
        return read_file(proof_file)

    # Restore state from disk
    if resume_info["has_progress"]:
        state.attempt = resume_info["attempt"]
        state.revision = resume_info["revision"]
        state.proof = resume_info["proof"]
        state.attempt_history = resume_info["attempt_history"]
        if resume_info["decomposition"]:
            state.decomposition = resume_info["decomposition"]

        decomp_logger.log(
            f"RESUMING: attempt={state.attempt}, revision={state.revision}, proof={state.proof}, "
            f"resume_point={resume_point}"
        )
        decomp_logger.update_status(
            state="RESUMING",
            attempt=state.attempt,
            revision=state.revision,
            proof=state.proof,
            recent_activity=f"Resuming from {resume_point}"
        )
    else:
        decomp_logger.log("Starting decomposition-based proof (fresh)")
        decomp_logger.update_status(
            state="INITIALIZING",
            attempt=1,
            revision=1,
            proof=1,
            recent_activity="Initializing decomposition prover"
        )

    os.makedirs(state.get_attempt_dir(), exist_ok=True)
    os.makedirs(state.get_revision_dir(), exist_ok=True)
    os.makedirs(state.get_proof_dir(), exist_ok=True)
    state.ensure_plan_history()

    # --- Handle mid-pipeline resume points ---
    verification_feedback = ""
    regulator_guidance = ""

    # Handle decompose_revise resume: restore context from previous revision
    if resume_point == "decompose_revise" and state.revision > 1:
        prev_rev_dir = os.path.join(state.get_attempt_dir(), f"revision_{state.revision - 1}")
        # Find the last proof in the previous revision
        last_proof = _find_max_numbered_dir(prev_rev_dir, "proof_")
        if last_proof > 0:
            prev_proof_dir = os.path.join(prev_rev_dir, f"proof_{last_proof}")
            # Restore verification feedback
            detailed = read_file(os.path.join(prev_proof_dir, "detailed_verification.md"))
            structural = read_file(os.path.join(prev_proof_dir, "structural_verification.md"))
            if detailed or structural:
                verification_feedback = f"# Structural Verification\n\n{structural}\n\n---\n\n# Detailed Verification\n\n{detailed}"
            # Restore regulator guidance
            regulator_guidance = read_file(os.path.join(prev_proof_dir, "regulator_decision.md"))
            decomp_logger.log(f"RESUMING: Restored context from revision {state.revision - 1}")

    if resume_point == "verify_structural":
        # Have proof, need to run structural verification
        proof_file = os.path.join(state.get_proof_dir(), "proof.md")
        decomp_logger.log("RESUMING: Running structural verification")

        structural_report_file = await run_structural_verification(
            state=state,
            problem_file=problem_file,
            proof_file=proof_file,
            prompts_dir=prompts_dir,
            config=config,
            claude_opts=claude_opts,
            decomp_logger=decomp_logger,
            tracker=tracker,
        )

        # Get verdict from the structural report
        structural_verdict = await run_verdict(
            state=state,
            structural_report_file=structural_report_file,
            prompts_dir=prompts_dir,
            config=config,
            claude_opts=claude_opts,
            mode="STRUCTURAL",
            decomp_logger=decomp_logger,
            tracker=tracker,
        )

        if structural_verdict == "DONE":
            resume_point = "verify_detailed"
        else:
            # Need regulator decision
            resume_point = "regulator"
            verification_feedback = read_file(structural_report_file)

    if resume_point == "verify_detailed":
        # Structural passed, need detailed verification
        proof_file = os.path.join(state.get_proof_dir(), "proof.md")
        structural_report_file = os.path.join(state.get_proof_dir(), "structural_verification.md")
        decomp_logger.log("RESUMING: Running detailed verification")

        detailed_report_file = await run_detailed_verification(
            state=state,
            problem_file=problem_file,
            proof_file=proof_file,
            structural_report_file=structural_report_file,
            prompts_dir=prompts_dir,
            config=config,
            claude_opts=claude_opts,
            decomp_logger=decomp_logger,
            tracker=tracker,
        )

        # Get final verdict from both reports
        final_verdict = await run_verdict(
            state=state,
            structural_report_file=structural_report_file,
            prompts_dir=prompts_dir,
            config=config,
            claude_opts=claude_opts,
            mode="FINAL",
            detailed_report_file=detailed_report_file,
            decomp_logger=decomp_logger,
            tracker=tracker,
        )

        if final_verdict == "DONE":
            decomp_logger.log("Proof verification PASSED on resume. Done!")
            decomp_logger.update_status(
                state="COMPLETED",
                recent_activity="Proof completed and verified successfully (resumed)"
            )
            return read_file(proof_file)

        # Need regulator decision
        resume_point = "regulator"
        structural_report = read_file(structural_report_file)
        detailed_report = read_file(detailed_report_file)
        verification_feedback = f"# Structural Verification\n\n{structural_report}\n\n---\n\n# Detailed Verification\n\n{detailed_report}"

    if resume_point == "regulator":
        # Need to call regulator
        proof_file = os.path.join(state.get_proof_dir(), "proof.md")
        decomp_logger.log("RESUMING: Calling regulator after verification failure")

        # If verification_feedback is empty (initial resume at "regulator"), read from files
        proof_dir = state.get_proof_dir()
        structural_report = read_file(os.path.join(proof_dir, "structural_verification.md"))
        detailed_report = read_file(os.path.join(proof_dir, "detailed_verification.md"))
        if not verification_feedback and (structural_report or detailed_report):
            verification_feedback = f"# Structural Verification\n\n{structural_report}\n\n---\n\n# Detailed Verification\n\n{detailed_report}"
        # Infer which phase produced the failure: detailed if its report
        # exists on disk, otherwise structural.
        resume_phase = "detailed" if detailed_report else "structural"

        decision = await run_regulator(
            state=state,
            verification_report=verification_feedback,
            proof_file=proof_file,
            config=config,
            prompts_dir=prompts_dir,
            claude_opts=claude_opts,
            decomp_logger=decomp_logger,
            tracker=tracker,
            verification_phase=resume_phase,
        )

        # Get regulator guidance from the decision file
        regulator_decision_file = os.path.join(state.get_proof_dir(), "regulator_decision.md")
        regulator_guidance = read_file(regulator_decision_file)

        if decision == "REVISE_PROOF":
            state.new_proof()
            resume_point = "prove"
        elif decision == "REVISE_PLAN":
            state.new_revision()
            resume_point = "decompose_revise"
        else:  # REWRITE
            state.new_attempt()
            resume_point = "decompose"

    # --- Main loop ---
    while state.attempt <= max_decompositions:
        # === Step 1: Create/Revise/Rewrite decomposition ===
        if resume_point in ("fresh", "decompose"):
            mode = "CREATE" if state.attempt == 1 and not resume_info["has_progress"] else "REWRITE"
            decomp_logger.log(f"Running decomposer in {mode} mode (attempt {state.attempt})")

            decomposition = await run_decomposer(
                state=state,
                problem_file=problem_file,
                related_work_file=related_work_file,
                prompts_dir=prompts_dir,
                config=config,
                claude_opts=claude_opts,
                mode=mode,
                regulator_guidance=regulator_guidance,
                decomp_logger=decomp_logger,
                tracker=tracker,
            )
            regulator_guidance = ""  # Clear after use

        elif resume_point == "decompose_revise":
            decomp_logger.log(f"Running decomposer in REVISE mode (attempt {state.attempt})")

            decomposition = await run_decomposer(
                state=state,
                problem_file=problem_file,
                related_work_file=related_work_file,
                prompts_dir=prompts_dir,
                config=config,
                claude_opts=claude_opts,
                mode="REVISE",
                verification_feedback=verification_feedback,
                regulator_guidance=regulator_guidance,
                decomp_logger=decomp_logger,
                tracker=tracker,
            )
            verification_feedback = ""
            regulator_guidance = ""

        else:
            # Resuming with existing decomposition
            decomposition = state.decomposition
            if not decomposition:
                try:
                    decomposition = state.load_decomposition()
                except yaml.YAMLError as e:
                    decomp_logger.log(f"Warning: Decomposition file has invalid YAML: {e}")
                    decomposition = None
            if not decomposition:
                # Safety fallback: decomposition missing or corrupted, start fresh
                decomp_logger.log("Warning: Decomposition missing on resume, creating new one")
                state.proof = 1  # Reset proof counter since we're starting fresh
                decomposition = await run_decomposer(
                    state=state,
                    problem_file=problem_file,
                    related_work_file=related_work_file,
                    prompts_dir=prompts_dir,
                    config=config,
                    claude_opts=claude_opts,
                    mode="CREATE",
                    decomp_logger=decomp_logger,
                    tracker=tracker,
                )

        key_steps = decomposition.get("key_steps", [])
        total_steps = len(decomposition.get("steps", []))
        decomp_logger.log(f"Decomposition: {total_steps} steps, {len(key_steps)} key steps")

        # === Step 2-5: Proof attempts loop ===
        while state.proof <= max_proof_attempts:
            proof_dir = state.get_proof_dir()
            os.makedirs(proof_dir, exist_ok=True)
            proof_file = os.path.join(proof_dir, "proof.md")
            proof_missing = ""  # set when the prover produced no document
            proof_partial = ""  # set when checkpoints survived a truncation
            proof_present_steps: list[str] = []
            proof_missing_steps: list[str] = []

            # === Step 2: Run single prover ===
            if resume_point in ("fresh", "decompose", "decompose_revise", "prove"):
                decomp_logger.log(f"Running single prover (attempt {state.attempt}, revision {state.revision}, proof {state.proof})")
                decomp_logger.update_status(
                    state="PROVING",
                    attempt=state.attempt,
                    revision=state.revision,
                    proof=state.proof,
                    recent_activity=f"Writing proof (proof {state.proof})"
                )

                try:
                    proof = await run_single_prover(
                        state=state,
                        problem_file=problem_file,
                        related_work_file=related_work_file,
                        prompts_dir=prompts_dir,
                        config=config,
                        claude_opts=claude_opts,
                        decomp_logger=decomp_logger,
                        tracker=tracker,
                    )
                except ProofIncomplete as exc:
                    proof_missing = str(exc)
                    proof_partial = exc.partial_file
                    proof_present_steps = exc.present
                    proof_missing_steps = exc.missing
                except ProofNotProduced as exc:
                    proof_missing = str(exc)

            resume_point = "verify_structural"  # Normal flow continues to verification

            if proof_missing:
                # Nothing to verify. Either the prover produced no document at
                # all, or it produced part of one and the session ended. Both
                # are interruptions, so a verification call would spend a full
                # model round-trip describing one as a mathematical failure, and
                # the regulator would then reason about a report with nothing
                # behind it. Give the regulator the cause instead.
                structural_report_file = ""
                structural_verdict = "CONTINUE"

                if proof_partial:
                    decomp_logger.log(
                        "Skipping verification: the session ended after "
                        f"{len(proof_present_steps)} of "
                        f"{len(proof_present_steps) + len(proof_missing_steps)} "
                        f"steps (missing {', '.join(proof_missing_steps)})"
                    )
                    proof_file = proof_partial
                    verification_feedback = (
                        "No verification report exists because the prover "
                        "session ended before the proof was finished. This is an "
                        "interrupted session, not a failed argument: do not "
                        "diagnose the missing steps as errors in the plan, and do "
                        "not read the truncation as evidence that the approach is "
                        "wrong.\n\n"
                        "Steps written before the interruption: "
                        f"{', '.join(proof_present_steps)}.\n"
                        f"Steps still missing: {', '.join(proof_missing_steps)}.\n"
                        f"The written steps are assembled at {proof_partial} and "
                        "may be reused: a new proof attempt can keep them and "
                        "continue from the first missing step instead of starting "
                        "over.\n\n" + proof_missing
                    )
                else:
                    decomp_logger.log(
                        "Skipping verification: the prover produced no proof document"
                    )
                    verification_feedback = (
                        "No verification report exists because the prover produced "
                        "no proof document to verify. It neither wrote proof.md, nor "
                        "left any step checkpoints, nor returned a response "
                        "containing any STEP heading -- the signature of a session "
                        "that ended before the write (context or output limit, CLI "
                        "crash, or kill).\n\n" + proof_missing
                    )
            else:
                # === Step 3: Structural verification ===
                decomp_logger.log("Running structural verification")
                decomp_logger.update_status(
                    state="VERIFYING_STRUCTURAL",
                    recent_activity="Running structural verification (Phases 1-5)"
                )

                structural_report_file = await run_structural_verification(
                    state=state,
                    problem_file=problem_file,
                    proof_file=proof_file,
                    prompts_dir=prompts_dir,
                    config=config,
                    claude_opts=claude_opts,
                    decomp_logger=decomp_logger,
                    tracker=tracker,
                )

                # === Step 3b: Structural verdict ===
                decomp_logger.log("Running structural verdict check...")
                structural_verdict = await run_verdict(
                    state=state,
                    structural_report_file=structural_report_file,
                    prompts_dir=prompts_dir,
                    config=config,
                    claude_opts=claude_opts,
                    mode="STRUCTURAL",
                    decomp_logger=decomp_logger,
                    tracker=tracker,
                )

                if structural_verdict == "CONTINUE":
                    # Get feedback and consult regulator
                    verification_feedback = read_file(structural_report_file)
                    decomp_logger.log("Structural verification FAILED. Consulting regulator...")

            if structural_verdict == "CONTINUE":
                decision = await run_regulator(
                    state=state,
                    verification_report=verification_feedback,
                    proof_file=proof_file,
                    config=config,
                    prompts_dir=prompts_dir,
                    claude_opts=claude_opts,
                    decomp_logger=decomp_logger,
                    tracker=tracker,
                    verification_phase="structural",
                )

                regulator_decision_file = os.path.join(proof_dir, "regulator_decision.md")
                regulator_guidance = read_file(regulator_decision_file)

                if decision == "REVISE_PROOF":
                    decomp_logger.log("Regulator: REVISE_PROOF - trying new proof with same plan")
                    state.new_proof()
                    resume_point = "prove"
                    continue  # Continue proof attempts loop

                elif decision == "REVISE_PLAN":
                    decomp_logger.log("Regulator: REVISE_PLAN - revising decomposition")
                    state.new_revision()
                    resume_point = "decompose_revise"
                    break  # Break to outer loop for decomposition revision

                else:  # REWRITE
                    decomp_logger.log("Regulator: REWRITE - starting new decomposition attempt")
                    state.new_attempt()
                    resume_point = "decompose"
                    break  # Break to outer loop for new attempt

            # === Step 4: Detailed verification ===
            decomp_logger.log("Structural verdict DONE. Running detailed verification...")
            decomp_logger.update_status(
                state="VERIFYING_DETAILED",
                recent_activity="Running detailed verification (Phase 6)"
            )

            detailed_report_file = await run_detailed_verification(
                state=state,
                problem_file=problem_file,
                proof_file=proof_file,
                structural_report_file=structural_report_file,
                prompts_dir=prompts_dir,
                config=config,
                claude_opts=claude_opts,
                decomp_logger=decomp_logger,
                tracker=tracker,
            )

            # === Step 5: Final verdict ===
            decomp_logger.log("Running final verdict check...")
            final_verdict = await run_verdict(
                state=state,
                structural_report_file=structural_report_file,
                prompts_dir=prompts_dir,
                config=config,
                claude_opts=claude_opts,
                mode="FINAL",
                detailed_report_file=detailed_report_file,
                decomp_logger=decomp_logger,
                tracker=tracker,
            )

            if final_verdict == "DONE":
                # === Success! ===
                decomp_logger.log("Final verdict: DONE. Proof completed successfully!")
                decomp_logger.update_status(
                    state="COMPLETED",
                    recent_activity="Proof completed and verified successfully"
                )
                return read_file(proof_file)

            # === Step 6: Verification failed - consult regulator ===
            structural_report = read_file(structural_report_file)
            detailed_report = read_file(detailed_report_file)
            verification_feedback = f"# Structural Verification\n\n{structural_report}\n\n---\n\n# Detailed Verification\n\n{detailed_report}"

            decomp_logger.log("Final verdict: CONTINUE. Consulting regulator...")

            decision = await run_regulator(
                state=state,
                verification_report=verification_feedback,
                proof_file=proof_file,
                config=config,
                prompts_dir=prompts_dir,
                claude_opts=claude_opts,
                decomp_logger=decomp_logger,
                tracker=tracker,
                verification_phase="detailed",
            )

            regulator_decision_file = os.path.join(proof_dir, "regulator_decision.md")
            regulator_guidance = read_file(regulator_decision_file)

            if decision == "REVISE_PROOF":
                decomp_logger.log("Regulator: REVISE_PROOF - trying new proof with same plan")
                state.new_proof()
                resume_point = "prove"
                continue  # Continue proof attempts loop

            elif decision == "REVISE_PLAN":
                decomp_logger.log("Regulator: REVISE_PLAN - revising decomposition")
                state.new_revision()
                resume_point = "decompose_revise"
                break  # Break to outer loop for decomposition revision

            else:  # REWRITE
                decomp_logger.log("Regulator: REWRITE - starting new decomposition attempt")
                state.new_attempt()
                resume_point = "decompose"
                break  # Break to outer loop for new attempt

        # Check if we exhausted proof attempts without breaking for revision/rewrite
        if state.proof > max_proof_attempts:
            # Try to escalate: first to REVISE_PLAN, then to REWRITE
            # Clear stale regulator_guidance from REVISE_PROOF decisions and provide
            # appropriate guidance for the escalation type
            if state.revision < max_revisions:
                decomp_logger.log(f"Max proof attempts ({max_proof_attempts}) exhausted, triggering plan revision")
                state.new_revision()
                resume_point = "decompose_revise"
                # Provide synthetic guidance for plan revision (not stale REVISE_PROOF guidance)
                regulator_guidance = (
                    f"# Automatic Escalation to Plan Revision\n\n"
                    f"All {max_proof_attempts} proof attempts for the previous revision failed verification. "
                    f"The repeated failures suggest structural issues with the decomposition plan rather than "
                    f"execution errors. Please revise the plan to address the persistent verification failures "
                    f"shown in the verification feedback above."
                )
            else:
                decomp_logger.log(f"Max revisions ({max_revisions}) exhausted, triggering rewrite")
                state.new_attempt()
                resume_point = "decompose"
                # Provide synthetic guidance for rewrite
                regulator_guidance = (
                    f"# Automatic Escalation to Complete Rewrite\n\n"
                    f"All {max_revisions} plan revisions have been exhausted without success. "
                    f"The fundamental proof strategy appears to be flawed. Please design a "
                    f"completely different approach to prove this problem."
                )

        # If we broke out for REVISE_PLAN, check revision limit
        if resume_point == "decompose_revise":
            if state.revision > max_revisions:
                decomp_logger.log(f"Max revisions ({max_revisions}) exhausted, triggering rewrite")
                state.new_attempt()
                resume_point = "decompose"
                # Update guidance for rewrite (was set for plan revision)
                regulator_guidance = (
                    f"# Escalation from Plan Revision to Complete Rewrite\n\n"
                    f"The regulator suggested revising the plan, but all {max_revisions} revision slots "
                    f"have been used. A completely new proof strategy is needed. "
                    f"The previous plan revision guidance was:\n\n{regulator_guidance}"
                )
            else:
                continue  # Continue with same attempt, revised plan

        # Otherwise (REWRITE or exhausted), check if we have more attempts
        if state.attempt > max_decompositions:
            break

    decomp_logger.log("All decomposition attempts exhausted - running final failure analysis")

    # Find the last proof and verification for FINAL mode regulator
    # We need to step back since state may have incremented past the last valid proof
    last_proof_dir = None
    for a in range(state.attempt, 0, -1):
        attempt_dir = os.path.join(state.decomp_dir, f"attempt_{a}")
        if not os.path.isdir(attempt_dir):
            continue
        max_rev = _find_max_numbered_dir(attempt_dir, "revision_")
        for r in range(max_rev, 0, -1):
            revision_dir = os.path.join(attempt_dir, f"revision_{r}")
            max_p = _find_max_numbered_dir(revision_dir, "proof_")
            if max_p > 0:
                last_proof_dir = os.path.join(revision_dir, f"proof_{max_p}")
                break
        if last_proof_dir:
            break

    if last_proof_dir:
        last_proof_file = os.path.join(last_proof_dir, "proof.md")
        # Get the last verification report (prefer detailed over structural)
        last_verification = read_file(os.path.join(last_proof_dir, "detailed_verification.md"))
        if not last_verification:
            last_verification = read_file(os.path.join(last_proof_dir, "structural_verification.md"))
        if not last_verification:
            last_verification = "No verification report found for last attempt."

        # Call regulator in FINAL mode for failure analysis
        await run_regulator(
            state=state,
            verification_report=last_verification,
            proof_file=last_proof_file,
            config=config,
            prompts_dir=prompts_dir,
            claude_opts=claude_opts,
            decomp_logger=decomp_logger,
            tracker=tracker,
            mode="FINAL",
            verification_phase="final",
        )

    decomp_logger.update_status(
        state="FAILED",
        recent_activity="All decomposition attempts exhausted - failure analysis written"
    )

    return ""


# ---------------------------------------------------------------------------
# Entry point for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("Decomposition prover module loaded.")
    print("Use run_decomposition_prover() from pipeline.py to run.")
