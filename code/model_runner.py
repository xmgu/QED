#!/usr/bin/env python3
"""Unified multi-model runner for Claude, Codex, and Gemini.

Provides async wrappers around each provider's CLI, returning response text
and feeding token usage into the pipeline's TokenTracker.

All three providers are invoked via their respective CLIs (subprocess),
wrapped in ``asyncio`` executors so the main event loop stays non-blocking.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from compat import decode_kwargs, resolve_cli, run_cli_async  # noqa: E402


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------
#
# ``error_type`` used to name the *mechanism* that failed -- ``non_zero_exit``,
# ``empty_response`` -- which says nothing about what to do next. When a call
# ends without a usable result the only useful question is *why*, because the
# remedies are different: shrink the prompt, raise the output ceiling, pay the
# bill, or retry. So failures are classified by cause.

TOKEN_CONTEXT = "token_context"      # prompt does not fit the context window
TOKEN_OUTPUT = "token_output"        # response cut off at the output ceiling
QUOTA = "quota"                      # credits, rate limit or overload
SESSION_DIED = "session_died"        # process ended, produced nothing usable
EMPTY_RESPONSE = "empty_response"
INCOMPLETE_TURN = "incomplete_turn"  # emitted words, never finished the turn
UNKNOWN = "unknown"

#: Causes that mean "another attempt costs tokens we do not have". These are
#: the only ones allowed to stop the whole run; everything else stays a
#: per-call error that the proof loop already knows how to retry.
TOKEN_CATEGORIES = (TOKEN_CONTEXT, TOKEN_OUTPUT, QUOTA)

CATEGORY_LABELS = {
    TOKEN_CONTEXT: "context window exceeded",
    TOKEN_OUTPUT: "output truncated at the token ceiling",
    QUOTA: "quota or rate limit exhausted",
    SESSION_DIED: "session died without producing a result",
    EMPTY_RESPONSE: "empty response",
    INCOMPLETE_TURN: "turn never completed",
    UNKNOWN: "unclassified failure",
}

_CONTEXT_MARKERS = (
    "context length", "context_length_exceeded", "context window",
    "maximum context", "prompt is too long", "prompt too long",
    "too many tokens", "exceeds the maximum", "input is too long",
    "token limit exceeded", "reduce the length of the messages",
)
_OUTPUT_MARKERS = (
    "max_tokens", "max output tokens", "maximum output", "output limit",
    "output token limit", "response was truncated", "truncated by",
    "finish_reason: length", "finish_reason='length'",
)
_QUOTA_MARKERS = (
    "insufficient_quota", "insufficient quota", "quota exceeded",
    "exceeded your current quota", "out of credits", "credit balance",
    "rate limit", "rate_limit", "429", "resource_exhausted",
    "resource exhausted", "overloaded", "too many requests",
)


def _marker_re(markers: tuple[str, ...]) -> re.Pattern:
    """Compile markers into one case-insensitive regex.

    A bare ``429`` would match inside any number in a proof, so it is anchored
    to digit boundaries; the rest are distinctive enough as literals.
    """
    parts = [r"(?<!\d)429(?!\d)" if m == "429" else re.escape(m) for m in markers]
    return re.compile("|".join(parts), re.IGNORECASE)


_CONTEXT_RE = _marker_re(_CONTEXT_MARKERS)
_OUTPUT_RE = _marker_re(_OUTPUT_MARKERS)
_QUOTA_RE = _marker_re(_QUOTA_MARKERS)


def _one_line(text: str, limit: int = 300) -> str:
    """Collapse arbitrary output into a single readable line."""
    return " ".join(str(text).split())[:limit]


def classify_failure(
    provider: str,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
    error_events: list[str] | None = None,
    completed: bool | None = None,
) -> tuple[str, list[str]]:
    """Classify a call that produced no usable result.

    Returns ``(category, evidence)``. ``evidence`` is the short list of signals
    the decision rests on; it goes into the error message and the halt report so
    the caller never has to take the category on trust.

    Order matters. A quota message often mentions the request size too, and a
    truncated response arrives as an error event, so the most specific cause has
    to be tested first.
    """
    evidence = [line for line in (error_events or []) if line]
    haystack = "\n".join(evidence + [stderr or "", (stdout or "")[-4000:]])

    for regex, category in ((_CONTEXT_RE, TOKEN_CONTEXT),
                            (_OUTPUT_RE, TOKEN_OUTPUT),
                            (_QUOTA_RE, QUOTA)):
        found = regex.search(haystack)
        if found:
            evidence.append(f"matched {found.group(0)!r}")
            return category, evidence

    if returncode not in (None, 0):
        evidence.append(f"exit code {returncode}")
    if completed is False:
        evidence.append("no completion event")
    if evidence:
        return SESSION_DIED, evidence
    if completed is True:
        return EMPTY_RESPONSE, evidence
    return UNKNOWN, evidence


def save_failure_artifact(
    working_dir: str,
    call_name: str,
    *,
    provider: str,
    model: str = "",
    category: str = UNKNOWN,
    evidence: list[str] | None = None,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
    elapsed: float | None = None,
) -> str:
    """Write the raw output of a failed call beside the run's artifacts.

    Until this existed the raw output was simply discarded. The four real
    crashes in ``run_20261023_103103`` left an empty stderr and nothing else, so
    afterwards there was no way to tell a dead session from an exhausted window
    -- the cause had to be guessed. A classifier can only be as good as the
    evidence it is handed, and this file is the only place that evidence
    survives.

    Returns the path written, or ``""`` if it could not be written.
    """
    try:
        dump_dir = os.path.join(working_dir, "runner_failures")
        os.makedirs(dump_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", call_name or provider).strip("_")[:60]
        path = os.path.join(dump_dir, f"{stamp}_{safe or provider}.log")

        def block(title: str, text: str) -> list[str]:
            text = (text or "").strip()
            if not text:
                return [f"## {title}", "", "(empty)", ""]
            lines = text.splitlines()
            if len(lines) > 300:
                lines = [f"... ({len(lines) - 300} earlier lines omitted)"] + lines[-300:]
            body = "\n".join(lines)
            if len(body) > 120000:
                body = "... (truncated)\n" + body[-120000:]
            return [f"## {title}", "", "```", body, "```", ""]

        header = [
            f"# Failure: {CATEGORY_LABELS.get(category, category)}",
            "",
            f"**Category:** `{category}`",
            f"**Provider:** {provider}" + (f" / {model}" if model else ""),
            f"**Call:** {call_name or '(unnamed)'}",
            f"**Exit code:** {returncode}",
            f"**Elapsed:** " + (f"{elapsed:.0f}s" if elapsed is not None else "unknown"),
            f"**When:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## Evidence",
            "",
        ]
        header += [f"- {_one_line(e)}" for e in evidence] if evidence else ["- (none)"]
        header.append("")

        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(header + block("Stderr", stderr)
                               + block("Stdout", stdout)) + "\n")
        return path
    except OSError:
        return ""


def _runner_failure(
    *,
    provider: str,
    model: str,
    call_name: str,
    working_dir: str,
    category: str,
    evidence: list[str],
    message: str,
    exit_code: int | None,
    stderr: str,
    stdout: str,
    elapsed: float | None,
) -> "ModelRunnerError":
    """Build the exception for a failed call, after preserving its evidence."""
    raw_log = save_failure_artifact(
        working_dir, call_name, provider=provider, model=model,
        category=category, evidence=evidence, stdout=stdout, stderr=stderr,
        returncode=exit_code, elapsed=elapsed,
    )
    cls = TokenExhausted if category in TOKEN_CATEGORIES else ModelRunnerError
    return cls(
        provider=provider,
        error_type=category,
        message=message,
        exit_code=exit_code,
        stderr=stderr,
        stdout=stdout,
        category=category,
        evidence=evidence,
        raw_log=raw_log,
        model=model,
        call_name=call_name,
        elapsed=elapsed,
    )


class ModelRunnerError(Exception):
    """Raised when a model runner encounters a fatal error.

    Attributes:
        provider: The model provider (claude, codex, gemini).
        error_type: Cause of the failure (see the ``*`` constants above).
        message: Human-readable error message.
        exit_code: Process exit code (if applicable).
        stderr: Stderr output from the CLI (if any).
        stdout: Raw stdout (for debugging).
        category: Classified cause; mirrors ``error_type``.
        evidence: Signals the classification rests on.
        raw_log: Path to the file holding the full raw output.
    """

    def __init__(
        self,
        provider: str,
        error_type: str,
        message: str,
        exit_code: int | None = None,
        stderr: str = "",
        stdout: str = "",
        *,
        category: str = "",
        evidence: list[str] | None = None,
        raw_log: str = "",
        model: str = "",
        call_name: str = "",
        elapsed: float | None = None,
    ):
        self.provider = provider
        self.error_type = error_type
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout
        self.category = category or error_type
        self.evidence = list(evidence or [])
        self.raw_log = raw_log
        self.model = model
        self.call_name = call_name
        self.elapsed = elapsed
        super().__init__(message)

    @property
    def is_token(self) -> bool:
        """True when the cause is a token budget, not a bad answer."""
        return self.category in TOKEN_CATEGORIES

    @property
    def label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    def headline(self) -> str:
        """One line naming the cause, the call and the model."""
        kind = "TOKEN EXHAUSTED" if self.is_token else "CALL FAILED"
        parts = [f"{kind} · {self.label}", self.provider]
        if self.model:
            parts[1] = f"{self.provider}/{self.model}"
        if self.call_name:
            parts.append(self.call_name)
        return " · ".join(parts)

    def banner(self) -> str:
        """Multi-line, evidence-backed summary for consoles and reports."""
        lines = [self.headline()]
        for item in self.evidence[:6]:
            lines.append(f"    evidence: {_one_line(item)}")
        if self.elapsed is not None:
            lines.append(f"    elapsed:  {self.elapsed:.0f}s")
        if self.raw_log:
            lines.append(f"    raw:      {self.raw_log}")
        return "\n".join(lines)

    def __str__(self):
        if self.is_token:
            return self.banner()
        parts = [f"[{self.provider}] {self.error_type}: {self.args[0]}"]
        if self.exit_code is not None:
            parts.append(f"exit_code={self.exit_code}")
        if self.stderr:
            # Truncate stderr for display
            stderr_preview = self.stderr[:500] + ("..." if len(self.stderr) > 500 else "")
            parts.append(f"stderr={stderr_preview!r}")
        text = " | ".join(parts)
        if self.evidence:
            text += "\n" + "\n".join(f"    evidence: {_one_line(e)}" for e in self.evidence[:4])
        if self.raw_log:
            text += f"\n    raw:      {self.raw_log}"
        return text

    def full_details(self) -> str:
        """Return full error details for logging to file."""
        lines = [
            f"# Model Runner Error",
            f"",
            f"**Provider:** {self.provider}",
            f"**Error Type:** {self.error_type}",
            f"**Message:** {self.args[0]}",
        ]
        if self.exit_code is not None:
            lines.append(f"**Exit Code:** {self.exit_code}")
        if self.stderr:
            lines.extend(["", "## Stderr", "```", self.stderr, "```"])
        if self.stdout:
            lines.extend(["", "## Stdout (first 2000 chars)", "```", self.stdout[:2000], "```"])
        return "\n".join(lines)


class TokenExhausted(ModelRunnerError):
    """The call failed because a token budget ran out.

    A distinct subclass rather than a flag, so a caller can write
    ``except TokenExhausted`` and stop the run, while every other runner error
    keeps its old meaning: "the model produced something unusable", which the
    proof loop already knows how to retry. Spending the remaining proof attempts
    on a context window that will not grow is not a mathematical decision, and
    should not be recorded as one.
    """


# ---------------------------------------------------------------------------
# Claude wrapper
# ---------------------------------------------------------------------------

async def run_claude_agent(
    prompt: str,
    working_dir: str,
    claude_opts: dict,
    logger=None,
    tracker=None,
    call_name: str = "",
    instructions: str | None = None,
) -> str:
    """Run the Claude CLI as a proof-search agent. Returns response text.

    Args:
        prompt: The full prompt string to send.
        working_dir: Directory the agent operates in (cwd for subprocess).
        claude_opts: Dict with keys: cli_path, model, env.
        logger: Optional PipelineLogger for streaming output.
        tracker: Optional TokenTracker for recording token usage.
        call_name: Human-readable label for this call.
        instructions: Optional system prompt to append.
    """
    cli_path = resolve_cli(claude_opts.get("cli_path", "claude"))
    model = claude_opts.get("model", "claude-fable-5-1")
    extra_env = claude_opts.get("env", {})

    cmd = [
        cli_path,
        "-p",
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", model,
    ]
    if instructions:
        cmd += ["--append-system-prompt", instructions]
    cmd.append(prompt)

    # Build environment: start from inherited env, strip vars that cause
    # provider cross-contamination, then add back only the configured ones.
    _PROVIDER_VARS = ("CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY",
                      "AWS_PROFILE", "ANTHROPIC_MODEL")
    env = {k: v for k, v in os.environ.items() if k not in _PROVIDER_VARS}
    env.update(extra_env)

    MAX_RETRIES = 3
    RETRY_BACKOFF = [30, 60, 120]  # seconds between retries

    start = datetime.now()
    if logger:
        logger.log(f"[Claude] Starting {call_name} (model={model})")

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        attempt_start = datetime.now()

        try:
            result = await run_cli_async(cmd, cwd=working_dir, env=env)
        except Exception as exc:
            elapsed = (datetime.now() - attempt_start).total_seconds()
            if logger:
                logger.log(f"[Claude] EXCEPTION (attempt {attempt}/{MAX_RETRIES}): "
                           f"{type(exc).__name__}: {exc}")
            last_error = _runner_failure(
                provider="claude", model=model, call_name=call_name,
                working_dir=working_dir, category="subprocess_error",
                evidence=[f"{type(exc).__name__}: {exc}"],
                message=f"Failed to execute Claude CLI: {type(exc).__name__}: {exc}",
                exit_code=None, stderr="", stdout="", elapsed=elapsed,
            )
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[attempt - 1]
                if logger:
                    logger.log(f"[Claude] Retrying in {wait}s...")
                await asyncio.sleep(wait)
                continue
            if tracker:
                tracker.record(call_name or "claude", 0, 0,
                               (datetime.now() - start).total_seconds(),
                               provider="claude", model=model,
                               outcome="subprocess_error")
            raise last_error

        elapsed = (datetime.now() - attempt_start).total_seconds()

        # Log stderr if present (contains error messages from CLI)
        if result.stderr and result.stderr.strip() and logger:
            logger.log(f"[Claude] stderr:\n{result.stderr.strip()}")

        # --- Parse JSON output ---
        response = ""
        input_tokens = 0
        output_tokens = 0
        json_parse_error = None
        is_error = False
        subtype = ""

        try:
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                data = {}
            response = data.get("result") or ""
            is_error = bool(data.get("is_error"))
            subtype = str(data.get("subtype") or "")

            for _, model_stats in data.get("modelUsage", {}).items():
                input_tokens += model_stats.get("inputTokens", 0)
                output_tokens += model_stats.get("outputTokens", 0)
        except (json.JSONDecodeError, ValueError) as exc:
            json_parse_error = str(exc)
            if logger:
                logger.log(f"[Claude] JSON parse error: {exc}")
                if result.stdout.strip():
                    logger.log(f"[Claude] Raw stdout (first 1000 chars): {result.stdout.strip()[:1000]}")
            response = result.stdout.strip()

        def _make_error(category: str, evidence: list[str], message: str):
            return _runner_failure(
                provider="claude", model=model, call_name=call_name,
                working_dir=working_dir, category=category, evidence=evidence,
                message=message, exit_code=result.returncode,
                stderr=result.stderr, stdout=result.stdout,
                elapsed=(datetime.now() - start).total_seconds(),
            )

        def _record(category: str):
            if tracker:
                tracker.record(call_name or "claude", input_tokens, output_tokens,
                               (datetime.now() - start).total_seconds(),
                               provider="claude", model=model, outcome=category)

        async def _retry_or_raise(error, category: str):
            if category in TOKEN_CATEGORIES:
                # Re-buying the same failure only makes it more expensive.
                if logger:
                    logger.log(f"[Claude] Not retrying: "
                               f"{CATEGORY_LABELS.get(category, category)}")
                _record(category)
                raise error
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[attempt - 1]
                if logger:
                    logger.log(f"[Claude] Retrying in {wait}s...")
                await asyncio.sleep(wait)
                return
            _record(category)
            raise error

        # Claude reports failures *inside* a zero-exit JSON envelope:
        #   {"is_error": true, "subtype": "error_max_turns", "result": "..."}
        # Left alone, `result` -- the error text -- becomes the agent's
        # "response", and a context overflow is filed as a bad proof.
        if is_error or subtype.startswith("error"):
            if subtype == "error_max_budget_usd":
                # A spend ceiling, not a window: the account cannot buy more.
                category, evidence = QUOTA, [f"subtype={subtype}"]
            else:
                # ``error_max_turns`` is a turn budget, not a token one. It is
                # left to the text classifier on purpose: calling it a token
                # failure would be a confident wrong answer.
                category, evidence = classify_failure(
                    "claude", stdout=result.stdout, stderr=result.stderr,
                    returncode=result.returncode,
                    error_events=[f"is_error=true subtype={subtype or 'unknown'}",
                                  response],
                    completed=False,
                )
            if logger:
                logger.log(f"[Claude] CLI reported {subtype or 'an error'} "
                           f"(attempt {attempt}/{MAX_RETRIES}, {category})")
            await _retry_or_raise(
                _make_error(category, evidence,
                            f"Claude reported {subtype or 'an error'}"),
                category,
            )
            continue

        # Check for non-zero exit code (indicates CLI failure) — retryable
        if result.returncode != 0:
            category, evidence = classify_failure(
                "claude", stdout=result.stdout, stderr=result.stderr,
                returncode=result.returncode,
                error_events=[f"subtype={subtype}"] if subtype else [],
                completed=False,
            )
            if logger:
                logger.log(f"[Claude] Non-zero exit code: {result.returncode} "
                           f"(attempt {attempt}/{MAX_RETRIES}, {category})")
            await _retry_or_raise(
                _make_error(category, evidence,
                            f"Claude CLI exited with code {result.returncode}"),
                category,
            )
            continue

        # Check for empty response (might indicate silent failure) — retryable
        if not response.strip():
            category, evidence = classify_failure(
                "claude", stdout=result.stdout, stderr=result.stderr,
                returncode=result.returncode,
                error_events=[f"JSON parse error: {json_parse_error}"] if json_parse_error else [],
                completed=True,
            )
            if logger:
                logger.log(f"[Claude] Empty response received "
                           f"(attempt {attempt}/{MAX_RETRIES}, {category})")
            await _retry_or_raise(
                _make_error(category, evidence,
                            "Claude returned empty response"
                            + (f" (JSON parse error: {json_parse_error})" if json_parse_error else "")),
                category,
            )
            continue

        # Success — break out of retry loop
        if attempt > 1 and logger:
            logger.log(f"[Claude] Succeeded on attempt {attempt}")
        break

    total_elapsed = (datetime.now() - start).total_seconds()
    if logger:
        logger.log(f"[Claude] Completed {call_name} in {total_elapsed:.0f}s "
                    f"({input_tokens} in / {output_tokens} out)")

    if tracker:
        tracker.record(call_name or "claude", input_tokens, output_tokens,
                       total_elapsed, provider="claude", model=model)

    return response


# ---------------------------------------------------------------------------
# Codex wrapper
# ---------------------------------------------------------------------------

async def run_codex_agent(
    prompt: str,
    working_dir: str,
    codex_config: dict,
    logger=None,
    tracker=None,
    call_name: str = "",
) -> str:
    """Run the Codex CLI as a proof-search agent. Returns response text.

    Args:
        prompt: The full prompt string to send.
        working_dir: Directory the agent operates in (cwd for subprocess).
        codex_config: Dict with keys: cli_path, model, reasoning_effort.
        logger: Optional PipelineLogger for streaming output.
        tracker: Optional TokenTracker for recording token usage.
        call_name: Human-readable label for this call.
    """
    cli_path = resolve_cli(codex_config.get("cli_path", "codex"))
    model = codex_config.get("model", "gpt-6-astra")
    reasoning = codex_config.get("reasoning_effort", "max")

    cmd = [
        cli_path,
        "--search",
        "-m", model,
        "-c", f'model_reasoning_effort="{reasoning}"',
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", working_dir,
        prompt,
    ]

    start = datetime.now()
    if logger:
        logger.log(f"[Codex] Starting {call_name} (model={model})")

    try:
        result = await run_cli_async(cmd, cwd=working_dir)
    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        if logger:
            logger.log(f"[Codex] EXCEPTION: {type(exc).__name__}: {exc}")
        if tracker:
            tracker.record(call_name or "codex", 0, 0, elapsed,
                           provider="codex", model=model, outcome="subprocess_error")
        raise _runner_failure(
            provider="codex", model=model, call_name=call_name,
            working_dir=working_dir, category="subprocess_error",
            evidence=[f"{type(exc).__name__}: {exc}"],
            message=f"Failed to execute Codex CLI: {type(exc).__name__}: {exc}",
            exit_code=None, stderr="", stdout="", elapsed=elapsed,
        )

    elapsed = (datetime.now() - start).total_seconds()

    # Log stderr if present (contains error messages from CLI)
    if result.stderr and result.stderr.strip() and logger:
        logger.log(f"[Codex] stderr:\n{result.stderr.strip()}")

    # --- Parse JSONL output (adapted from test_call.py:41-67) ---
    response = ""
    input_tokens = 0
    output_tokens = 0
    json_parse_error = None
    turn_completed = False
    messages_seen = 0
    error_events: list[str] = []

    try:
        lines = result.stdout.strip().split("\n")
        events = [json.loads(line) for line in lines if line.strip()]

        for event in events:
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    # Only the last message survives. Normally that is the
                    # final answer; when a session is cut off it is a
                    # mid-work narration, which callers must not mistake
                    # for a finished artifact.
                    response = item.get("text", "")
                    messages_seen += 1
            elif event.get("type") == "turn.completed":
                turn_completed = True
                usage = event.get("usage", {})
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
            elif event.get("type") in ("error", "turn.failed"):
                # The only place a JSONL session states its own cause. It used
                # to be dropped on the floor: only agent_message and
                # turn.completed were read, so a context overflow and a killed
                # process looked identical from the outside.
                detail = event.get("message") or (event.get("error") or {}).get("message")
                error_events.append(
                    f"{event.get('type')}: {detail or json.dumps(event)[:400]}")
    except (json.JSONDecodeError, ValueError) as exc:
        json_parse_error = str(exc)
        if logger:
            logger.log(f"[Codex] JSON parse error: {exc}")
            if result.stdout.strip():
                logger.log(f"[Codex] Raw stdout (first 1000 chars): {result.stdout.strip()[:1000]}")
        # JSON mode must produce an assistant message, not raw CLI diagnostics.
        response = ""

    # A turn that produced messages but never emitted turn.completed ended
    # early: the CLI hit its context or output ceiling, crashed, or was killed.
    # token_usage then records 0/0 (no usage event), and the surviving
    # response is whatever the last message happened to be.
    #
    # This is not fatal -- the caller's looks_like_proof decides whether the
    # text is an artifact. But the raw session is kept, because "emitted words,
    # then stopped with no usage event" is exactly the shape a swallowed
    # context error takes, and it is the only way to tell afterwards.
    outcome = "ok"
    if response.strip() and not turn_completed:
        outcome = INCOMPLETE_TURN
        raw = save_failure_artifact(
            working_dir, call_name, provider="codex", model=model,
            category=INCOMPLETE_TURN,
            evidence=[f"{messages_seen} message(s), no turn.completed event",
                      *error_events],
            stdout=result.stdout, stderr=result.stderr,
            returncode=result.returncode, elapsed=elapsed,
        )
        if logger:
            logger.log(
                f"[Codex] Warning: turn did not complete ({messages_seen} "
                "message(s) seen, no turn.completed event) — the captured "
                "response is the last message emitted, not necessarily a "
                "finished answer" + (f"; raw session kept at {raw}" if raw else "")
            )

    # A turn that failed for a token reason is not an answer, even if it
    # narrated something first. The last agent_message of a failed turn is
    # mid-work commentary; returning it would hand the caller a "response" that
    # looks like progress and files a budget problem as a bad proof.
    if error_events:
        category, evidence = classify_failure(
            "codex", stdout=result.stdout, stderr=result.stderr,
            returncode=result.returncode, error_events=error_events,
            completed=turn_completed,
        )
        if category in TOKEN_CATEGORIES:
            if logger:
                logger.log(f"[Codex] Failure: {category}")
            if tracker:
                tracker.record(call_name or "codex", input_tokens, output_tokens,
                               elapsed, provider="codex", model=model, outcome=category)
            raise _runner_failure(
                provider="codex", model=model, call_name=call_name,
                working_dir=working_dir, category=category, evidence=evidence,
                message=evidence[0] if evidence else "Codex turn failed",
                exit_code=result.returncode, stderr=result.stderr,
                stdout=result.stdout, elapsed=elapsed,
            )

    # The Codex CLI sometimes exits non-zero (e.g. "Reading additional input
    # from stdin..." warning → exit code 1) even when it produced a valid
    # agent_message. Treat the response payload as authoritative: a non-zero
    # exit is only fatal if we could not parse any usable response.
    if result.returncode != 0:
        if logger:
            logger.log(f"[Codex] Non-zero exit code: {result.returncode} "
                       f"(treating as warning since response was parsed)" if response.strip()
                       else f"[Codex] Non-zero exit code: {result.returncode}")
        if not response.strip():
            category, evidence = classify_failure(
                "codex", stdout=result.stdout, stderr=result.stderr,
                returncode=result.returncode, error_events=error_events,
                completed=turn_completed,
            )
            if tracker:
                tracker.record(call_name or "codex", input_tokens, output_tokens,
                               elapsed, provider="codex", model=model, outcome=category)
            raise _runner_failure(
                provider="codex", model=model, call_name=call_name,
                working_dir=working_dir, category=category, evidence=evidence,
                message=f"Codex CLI exited with code {result.returncode}",
                exit_code=result.returncode, stderr=result.stderr,
                stdout=result.stdout, elapsed=elapsed,
            )

    # Check for empty response (might indicate silent failure)
    if not response.strip():
        category, evidence = classify_failure(
            "codex", stdout=result.stdout, stderr=result.stderr,
            returncode=result.returncode, error_events=error_events,
            completed=turn_completed,
        )
        if logger:
            logger.log(f"[Codex] Empty response received ({category})")
        if tracker:
            tracker.record(call_name or "codex", input_tokens, output_tokens,
                           elapsed, provider="codex", model=model, outcome=category)
        raise _runner_failure(
            provider="codex", model=model, call_name=call_name,
            working_dir=working_dir, category=category, evidence=evidence,
            message="Codex returned empty response"
                    + (f" (JSON parse error: {json_parse_error})" if json_parse_error else ""),
            exit_code=result.returncode, stderr=result.stderr,
            stdout=result.stdout, elapsed=elapsed,
        )

    if logger:
        logger.log(f"[Codex] Completed {call_name} in {elapsed:.0f}s "
                    f"({input_tokens} in / {output_tokens} out)")

    if tracker:
        tracker.record(call_name or "codex", input_tokens, output_tokens,
                       elapsed, provider="codex", model=model, outcome=outcome)

    return response


# ---------------------------------------------------------------------------
# Gemini wrapper
# ---------------------------------------------------------------------------

async def run_gemini_agent(
    prompt: str,
    working_dir: str,
    gemini_config: dict,
    logger=None,
    tracker=None,
    call_name: str = "",
) -> str:
    """Run the Gemini CLI as a proof-search agent. Returns response text.

    Args:
        prompt: The full prompt string to send.
        working_dir: Directory the agent operates in (cwd for subprocess).
        gemini_config: Dict with keys: cli_path, model, api_key,
            approval_mode, thinking_level, thinking_budget.
        logger: Optional PipelineLogger for streaming output.
        tracker: Optional TokenTracker for recording token usage.
        call_name: Human-readable label for this call.
    """
    cli_path = resolve_cli(gemini_config.get("cli_path", "gemini"))
    model = gemini_config.get("model", "gemini-3.1-pro-preview")
    api_key = gemini_config.get("api_key", "")
    approval_mode = gemini_config.get("approval_mode", "yolo")
    thinking_level = gemini_config.get("thinking_level", "")
    thinking_budget = gemini_config.get("thinking_budget")

    # ``thinking_config`` used to be read here without ever being assigned, so
    # every Gemini call died with NameError before the CLI was even spawned.
    # That matters most in exactly the situation this module now reports: when
    # Codex runs out of tokens and the role is pointed at Gemini instead.
    thinking_config = {}
    if thinking_level:
        thinking_config["thinkingLevel"] = str(thinking_level).upper()
    elif thinking_budget is not None:
        try:
            thinking_config["thinkingBudget"] = int(thinking_budget)
        except (TypeError, ValueError):
            thinking_config = {}

    cmd = [
        cli_path,
        "-m", model,
        "--approval-mode", approval_mode,
        "-o", "json",   # JSON output for metadata extraction
        "-p", prompt,
    ]

    def _make_env():
        env = os.environ.copy()
        if api_key:
            env["GEMINI_API_KEY"] = api_key
        return env

    def _write_thinking_config(gemini_home):
        settings_dir = os.path.join(gemini_home, ".gemini")
        os.makedirs(settings_dir, exist_ok=True)
        settings_path = os.path.join(settings_dir, "settings.json")
        settings = {
            "modelConfigs": {
                "overrides": [
                    {
                        "match": {"model": model},
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
        return settings_path

    start = datetime.now()
    if logger:
        logger.log(f"[Gemini] Starting {call_name} (model={model})")

    try:
        if thinking_config:
            with tempfile.TemporaryDirectory(prefix="qed-gemini-home-") as gemini_home:
                _write_thinking_config(gemini_home)
                env = _make_env()
                env["GEMINI_CLI_HOME"] = gemini_home
                result = await run_cli_async(cmd, cwd=working_dir, env=env)
        else:
            result = await run_cli_async(cmd, cwd=working_dir, env=_make_env())
    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        if logger:
            logger.log(f"[Gemini] EXCEPTION: {type(exc).__name__}: {exc}")
        if tracker:
            tracker.record(call_name or "gemini", 0, 0, elapsed,
                           provider="gemini", model=model, outcome="subprocess_error")
        raise _runner_failure(
            provider="gemini", model=model, call_name=call_name,
            working_dir=working_dir, category="subprocess_error",
            evidence=[f"{type(exc).__name__}: {exc}"],
            message=f"Failed to execute Gemini CLI: {type(exc).__name__}: {exc}",
            exit_code=None, stderr="", stdout="", elapsed=elapsed,
        )

    elapsed = (datetime.now() - start).total_seconds()

    # Log stderr if present (contains error messages from CLI)
    if result.stderr and result.stderr.strip() and logger:
        logger.log(f"[Gemini] stderr:\n{result.stderr.strip()}")

    # --- Parse JSON output (adapted from test_call.py:74-121) ---
    response = ""
    input_tokens = 0
    output_tokens = 0
    json_parse_error = None
    error_events: list[str] = []

    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            data = {}
        response = data.get("response") or ""
        if data.get("error"):
            err = data["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else ""
            error_events.append(f"error {code}: {detail}")

        for _, model_stats in data.get("stats", {}).get("models", {}).items():
            tokens = model_stats.get("tokens", {})
            input_tokens += tokens.get("input", 0)
            output_tokens += tokens.get("candidates", 0)
            output_tokens += tokens.get("thoughts", 0)  # include thinking tokens
    except (json.JSONDecodeError, ValueError) as exc:
        json_parse_error = str(exc)
        if logger:
            logger.log(f"[Gemini] JSON parse error: {exc}")
            if result.stdout.strip():
                logger.log(f"[Gemini] Raw stdout (first 1000 chars): {result.stdout.strip()[:1000]}")
        response = result.stdout.strip()

    # An error envelope carries no response, so it is a failure even at exit 0.
    if error_events or (result.returncode != 0 and not response.strip()):
        category, evidence = classify_failure(
            "gemini", stdout=result.stdout, stderr=result.stderr,
            returncode=result.returncode, error_events=error_events,
            completed=False,
        )
        if logger:
            logger.log(f"[Gemini] Failure: {category}")
        if tracker:
            tracker.record(call_name or "gemini", input_tokens, output_tokens,
                           elapsed, provider="gemini", model=model, outcome=category)
        raise _runner_failure(
            provider="gemini", model=model, call_name=call_name,
            working_dir=working_dir, category=category, evidence=evidence,
            message=f"Gemini CLI exited with code {result.returncode}"
                    if result.returncode != 0 else "Gemini reported an error",
            exit_code=result.returncode, stderr=result.stderr,
            stdout=result.stdout, elapsed=elapsed,
        )

    # Check for empty response (might indicate silent failure)
    if not response.strip():
        category, evidence = classify_failure(
            "gemini", stdout=result.stdout, stderr=result.stderr,
            returncode=result.returncode, completed=True,
        )
        if logger:
            logger.log(f"[Gemini] Empty response received ({category})")
        if tracker:
            tracker.record(call_name or "gemini", input_tokens, output_tokens,
                           elapsed, provider="gemini", model=model, outcome=category)
        raise _runner_failure(
            provider="gemini", model=model, call_name=call_name,
            working_dir=working_dir, category=category, evidence=evidence,
            message="Gemini returned empty response"
                    + (f" (JSON parse error: {json_parse_error})" if json_parse_error else ""),
            exit_code=result.returncode, stderr=result.stderr,
            stdout=result.stdout, elapsed=elapsed,
        )

    if logger:
        logger.log(f"[Gemini] Completed {call_name} in {elapsed:.0f}s "
                    f"({input_tokens} in / {output_tokens} out)")

    if tracker:
        tracker.record(call_name or "gemini", input_tokens, output_tokens,
                       elapsed, provider="gemini", model=model)

    return response


# ---------------------------------------------------------------------------
# Per-agent override resolution
# ---------------------------------------------------------------------------

def resolve_agent_provider_config(
    full_config: dict,
    agent_role_cfg: dict,
) -> tuple[str, dict]:
    """Resolve a per-agent role config against the global provider section.

    Each agent role in config.yaml is a dict of the form::

        { provider: "codex", model: "gpt-5.5", reasoning_effort: "xhigh" }

    The provider name picks the global section (``codex:`` / ``gemini:`` /
    ``claude:``). Any other keys override the corresponding fields from the
    global section. Knobs not set on the agent fall back to global.

    For ``claude``, the global section contains nested
    ``subscription:`` / ``api_key:`` / ``bedrock:`` blocks; the per-agent
    ``model`` override (if any) overrides whichever block ``claude.provider``
    selects (the global ``claude.provider`` value still controls auth).

    Returns ``(provider, merged_provider_cfg)`` — ``merged_provider_cfg`` is
    a shallow copy of the global section with per-agent fields overlaid.
    """
    if not isinstance(agent_role_cfg, dict):
        raise ValueError(
            f"Agent role config must be a dict like "
            f"{{provider: 'codex', model: 'gpt-5.5'}}, got: {agent_role_cfg!r}"
        )
    provider = agent_role_cfg.get("provider")
    if not provider:
        raise ValueError(
            f"Agent role config is missing required 'provider' field: {agent_role_cfg!r}"
        )
    provider = provider.lower().strip()
    if provider not in ("claude", "codex", "gemini"):
        raise ValueError(
            f"Unknown provider {provider!r}; expected 'claude', 'codex', or 'gemini'."
        )

    overrides = {k: v for k, v in agent_role_cfg.items() if k != "provider"}
    global_section = full_config.get(provider, {})

    if provider == "claude":
        merged = {k: v for k, v in global_section.items()}
        auth_mode = merged.get("provider", "subscription")
        # Apply the per-agent model override into the active auth block
        if "model" in overrides:
            sub = dict(merged.get(auth_mode, {}))
            sub["model"] = overrides["model"]
            merged[auth_mode] = sub
        # Other claude-level overrides (cli_path, permission_mode) overlay directly
        for k, v in overrides.items():
            if k == "model":
                continue
            merged[k] = v
        return provider, merged

    # codex / gemini: flat dict merge
    merged = {**global_section, **overrides}
    return provider, merged


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

async def run_model(
    provider: str,
    prompt: str,
    working_dir: str,
    config: dict,
    *,
    claude_opts: dict | None = None,
    logger=None,
    tracker=None,
    call_name: str = "",
    instructions: str | None = None,
) -> str:
    """Dispatch a prompt to the specified model provider.

    Args:
        provider: One of "claude", "codex", "gemini".
        prompt: The full prompt string.
        working_dir: Agent's working directory.
        config: Full pipeline config dict (with claude/codex/gemini sections).
        claude_opts: Claude CLI options dict (required when provider="claude").
        logger: Optional PipelineLogger.
        tracker: Optional TokenTracker.
        call_name: Human-readable label.
        instructions: System instructions (used only for Claude).

    Returns:
        The agent's response text.
    """
    if provider == "claude":
        return await run_claude_agent(
            prompt, working_dir, claude_opts or {},
            logger=logger, tracker=tracker, call_name=call_name,
            instructions=instructions,
        )
    elif provider == "codex":
        return await run_codex_agent(
            prompt, working_dir, config.get("codex", {}),
            logger=logger, tracker=tracker, call_name=call_name,
        )
    elif provider == "gemini":
        return await run_gemini_agent(
            prompt, working_dir, config.get("gemini", {}),
            logger=logger, tracker=tracker, call_name=call_name,
        )
    else:
        raise ValueError(f"Unknown model provider: {provider!r}. "
                         f"Expected 'claude', 'codex', or 'gemini'.")


async def run_model_for_agent(
    agent_role_cfg: dict,
    prompt: str,
    working_dir: str,
    config: dict,
    *,
    claude_opts: dict | None = None,
    logger=None,
    tracker=None,
    call_name: str = "",
    instructions: str | None = None,
) -> str:
    """Dispatch using a per-agent role config (the dict-with-provider format).

    Resolves ``agent_role_cfg`` against the global provider section, then
    invokes :func:`run_model` with an effective config in which the chosen
    provider's section has been overlaid with the per-agent overrides.

    For ``claude``, also overlays the per-agent ``model`` into ``claude_opts``.
    """
    provider, merged_provider_cfg = resolve_agent_provider_config(
        config, agent_role_cfg
    )
    effective_config = dict(config)
    effective_config[provider] = merged_provider_cfg

    if provider == "claude":
        effective_claude_opts = dict(claude_opts or {})
        if "model" in agent_role_cfg:
            effective_claude_opts["model"] = agent_role_cfg["model"]
    else:
        effective_claude_opts = claude_opts

    return await run_model(
        provider,
        prompt,
        working_dir,
        effective_config,
        claude_opts=effective_claude_opts,
        logger=logger,
        tracker=tracker,
        call_name=call_name,
        instructions=instructions,
    )
