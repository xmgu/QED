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
import subprocess
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from compat import decode_kwargs, resolve_cli  # noqa: E402


class ModelRunnerError(Exception):
    """Raised when a model runner encounters a fatal error.

    Attributes:
        provider: The model provider (claude, codex, gemini).
        error_type: Category of error (subprocess_error, non_zero_exit, json_parse_error, empty_response).
        message: Human-readable error message.
        exit_code: Process exit code (if applicable).
        stderr: Stderr output from the CLI (if any).
        stdout: Raw stdout (for debugging).
    """

    def __init__(
        self,
        provider: str,
        error_type: str,
        message: str,
        exit_code: int | None = None,
        stderr: str = "",
        stdout: str = "",
    ):
        self.provider = provider
        self.error_type = error_type
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout
        super().__init__(message)

    def __str__(self):
        parts = [f"[{self.provider}] {self.error_type}: {self.args[0]}"]
        if self.exit_code is not None:
            parts.append(f"exit_code={self.exit_code}")
        if self.stderr:
            # Truncate stderr for display
            stderr_preview = self.stderr[:500] + ("..." if len(self.stderr) > 500 else "")
            parts.append(f"stderr={stderr_preview!r}")
        return " | ".join(parts)

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

    def _call():
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_dir,
            env=env,
            **decode_kwargs(),
        )

    MAX_RETRIES = 3
    RETRY_BACKOFF = [30, 60, 120]  # seconds between retries

    start = datetime.now()
    if logger:
        logger.log(f"[Claude] Starting {call_name} (model={model})")

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        attempt_start = datetime.now()

        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _call)
        except Exception as exc:
            elapsed = (datetime.now() - attempt_start).total_seconds()
            if logger:
                logger.log(f"[Claude] EXCEPTION (attempt {attempt}/{MAX_RETRIES}): "
                           f"{type(exc).__name__}: {exc}")
            last_error = ModelRunnerError(
                provider="claude",
                error_type="subprocess_error",
                message=f"Failed to execute Claude CLI: {type(exc).__name__}: {exc}",
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
                               provider="claude", model=model)
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

        try:
            data = json.loads(result.stdout)
            response = data.get("result", "")

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

        # Check for non-zero exit code (indicates CLI failure) — retryable
        if result.returncode != 0:
            if logger:
                logger.log(f"[Claude] Non-zero exit code: {result.returncode} "
                           f"(attempt {attempt}/{MAX_RETRIES})")
            last_error = ModelRunnerError(
                provider="claude",
                error_type="non_zero_exit",
                message=f"Claude CLI exited with code {result.returncode}",
                exit_code=result.returncode,
                stderr=result.stderr,
                stdout=result.stdout,
            )
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[attempt - 1]
                if logger:
                    logger.log(f"[Claude] Retrying in {wait}s...")
                await asyncio.sleep(wait)
                continue
            if tracker:
                tracker.record(call_name or "claude", input_tokens, output_tokens,
                               (datetime.now() - start).total_seconds(),
                               provider="claude", model=model)
            raise last_error

        # Check for empty response (might indicate silent failure) — retryable
        if not response.strip():
            if logger:
                logger.log(f"[Claude] Empty response received "
                           f"(attempt {attempt}/{MAX_RETRIES})")
            last_error = ModelRunnerError(
                provider="claude",
                error_type="empty_response",
                message="Claude returned empty response" + (f" (JSON parse error: {json_parse_error})" if json_parse_error else ""),
                exit_code=result.returncode,
                stderr=result.stderr,
                stdout=result.stdout,
            )
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[attempt - 1]
                if logger:
                    logger.log(f"[Claude] Retrying in {wait}s...")
                await asyncio.sleep(wait)
                continue
            if tracker:
                tracker.record(call_name or "claude", input_tokens, output_tokens,
                               (datetime.now() - start).total_seconds(),
                               provider="claude", model=model)
            raise last_error

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

    def _call():
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_dir,
            **decode_kwargs(),
        )

    start = datetime.now()
    if logger:
        logger.log(f"[Codex] Starting {call_name} (model={model})")

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _call)
    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        if logger:
            logger.log(f"[Codex] EXCEPTION: {type(exc).__name__}: {exc}")
        if tracker:
            tracker.record(call_name or "codex", 0, 0, elapsed,
                           provider="codex", model=model)
        raise ModelRunnerError(
            provider="codex",
            error_type="subprocess_error",
            message=f"Failed to execute Codex CLI: {type(exc).__name__}: {exc}",
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

    try:
        lines = result.stdout.strip().split("\n")
        events = [json.loads(line) for line in lines if line.strip()]

        for event in events:
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    response = item.get("text", "")
            elif event.get("type") == "turn.completed":
                usage = event.get("usage", {})
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
    except (json.JSONDecodeError, ValueError) as exc:
        json_parse_error = str(exc)
        if logger:
            logger.log(f"[Codex] JSON parse error: {exc}")
            if result.stdout.strip():
                logger.log(f"[Codex] Raw stdout (first 1000 chars): {result.stdout.strip()[:1000]}")
        # JSON mode must produce an assistant message, not raw CLI diagnostics.
        response = ""

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
            if tracker:
                tracker.record(call_name or "codex", input_tokens, output_tokens,
                               elapsed, provider="codex", model=model)
            raise ModelRunnerError(
                provider="codex",
                error_type="non_zero_exit",
                message=f"Codex CLI exited with code {result.returncode}",
                exit_code=result.returncode,
                stderr=result.stderr,
                stdout=result.stdout,
            )

    # Check for empty response (might indicate silent failure)
    if not response.strip():
        if logger:
            logger.log(f"[Codex] Empty response received")
        if tracker:
            tracker.record(call_name or "codex", input_tokens, output_tokens,
                           elapsed, provider="codex", model=model)
        raise ModelRunnerError(
            provider="codex",
            error_type="empty_response",
            message="Codex returned empty response" + (f" (JSON parse error: {json_parse_error})" if json_parse_error else ""),
            exit_code=result.returncode,
            stderr=result.stderr,
            stdout=result.stdout,
        )

    if logger:
        logger.log(f"[Codex] Completed {call_name} in {elapsed:.0f}s "
                    f"({input_tokens} in / {output_tokens} out)")

    if tracker:
        tracker.record(call_name or "codex", input_tokens, output_tokens,
                       elapsed, provider="codex", model=model)

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

    cmd = [
        cli_path,
        "-m", model,
        "--approval-mode", approval_mode,
        "-o", "json",   # JSON output for metadata extraction
        "-p", prompt,
    ]

    def _call():
        env = os.environ.copy()
        if api_key:
            env["GEMINI_API_KEY"] = api_key

        thinking_config = {}
        if thinking_level:
            thinking_config["thinkingLevel"] = thinking_level
        if thinking_budget is not None:
            thinking_config["thinkingBudget"] = thinking_budget

        if thinking_config:
            with tempfile.TemporaryDirectory(prefix="qed-gemini-home-") as gemini_home:
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
                env["GEMINI_CLI_HOME"] = gemini_home
                return subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=working_dir,
                    env=env,
                    **decode_kwargs(),
                )

        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_dir,
            env=env,
            **decode_kwargs(),
        )

    start = datetime.now()
    if logger:
        logger.log(f"[Gemini] Starting {call_name} (model={model})")

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _call)
    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        if logger:
            logger.log(f"[Gemini] EXCEPTION: {type(exc).__name__}: {exc}")
        if tracker:
            tracker.record(call_name or "gemini", 0, 0, elapsed,
                           provider="gemini", model=model)
        raise ModelRunnerError(
            provider="gemini",
            error_type="subprocess_error",
            message=f"Failed to execute Gemini CLI: {type(exc).__name__}: {exc}",
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

    try:
        data = json.loads(result.stdout)
        response = data.get("response", "")

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

    # Check for non-zero exit code (indicates CLI failure)
    if result.returncode != 0:
        if logger:
            logger.log(f"[Gemini] Non-zero exit code: {result.returncode}")
        if tracker:
            tracker.record(call_name or "gemini", input_tokens, output_tokens,
                           elapsed, provider="gemini", model=model)
        raise ModelRunnerError(
            provider="gemini",
            error_type="non_zero_exit",
            message=f"Gemini CLI exited with code {result.returncode}",
            exit_code=result.returncode,
            stderr=result.stderr,
            stdout=result.stdout,
        )

    # Check for empty response (might indicate silent failure)
    if not response.strip():
        if logger:
            logger.log(f"[Gemini] Empty response received")
        if tracker:
            tracker.record(call_name or "gemini", input_tokens, output_tokens,
                           elapsed, provider="gemini", model=model)
        raise ModelRunnerError(
            provider="gemini",
            error_type="empty_response",
            message="Gemini returned empty response" + (f" (JSON parse error: {json_parse_error})" if json_parse_error else ""),
            exit_code=result.returncode,
            stderr=result.stderr,
            stdout=result.stdout,
        )

    if logger:
        logger.log(f"[Gemini] Completed {call_name} in {elapsed:.0f}s "
                    f"({input_tokens} in / {output_tokens} out)")

    if tracker:
        tracker.record(call_name or "gemini", input_tokens, output_tokens,
                       elapsed, provider="gemini", model=model)

    return response


# ---------------------------------------------------------------------------
# DeepSeek Harness (dsh) wrapper
# ---------------------------------------------------------------------------

async def run_dsh_agent(
    prompt: str,
    working_dir: str,
    dsh_config: dict,
    logger=None,
    tracker=None,
    call_name: str = "",
) -> str:
    """Run the DeepSeek Harness CLI as a proof-search agent.

    Returns the response text.

    ``dsh`` is launched in its ``headless`` profile, which answers one
    task and exits. The prompt is fed through stdin rather than as a
    command-line argument: the rendered prompts reach 20-30 k characters
    and Windows caps a command line at 32767, so passing them as argv
    would fail there.

    The harness emits newline-delimited JSON events; the answer is the
    ``final`` event's ``text`` field.

    Args:
        prompt: The full prompt string.
        working_dir: Directory the agent operates in (cwd for subprocess).
        dsh_config: Dict with keys: cli_path, profile, model, api_key,
            permission_mode.
        logger: Optional PipelineLogger for streaming output.
        tracker: Optional TokenTracker for recording token usage.
        call_name: Human-readable label for this call.
    """
    cli_path = resolve_cli(dsh_config.get("cli_path", "dsh"))
    profile = dsh_config.get("profile", "headless")
    model = dsh_config.get("model", "deepseek-flash")
    api_key = dsh_config.get("api_key", "")
    permission_mode = dsh_config.get("permission_mode", "danger-full-access")

    cmd = [
        cli_path,
        "--profile", profile,
        "--json",
    ]

    def _call():
        env = os.environ.copy()
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
        # The harness reads these to decide what the agent may do. The
        # pipeline needs file read/write and shell access inside the
        # output directory, which is what "danger-full-access" grants.
        env["DSH_PERMISSION_MODE"] = permission_mode
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_dir,
            env=env,
            input=prompt,
            **decode_kwargs(),
        )

    start = datetime.now()
    if logger:
        logger.log(f"[DSH] Starting {call_name} (model={model})")

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _call)
    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        if logger:
            logger.log(f"[DSH] EXCEPTION: {type(exc).__name__}: {exc}")
        if tracker:
            tracker.record(call_name or "dsh", 0, 0, elapsed,
                           provider="dsh", model=model)
        raise ModelRunnerError(
            provider="dsh",
            error_type="subprocess_error",
            message=f"Failed to execute dsh CLI: {type(exc).__name__}: {exc}",
        )

    elapsed = (datetime.now() - start).total_seconds()

    if result.stderr and result.stderr.strip() and logger:
        logger.log(f"[DSH] stderr:\n{result.stderr.strip()}")

    # --- Parse the NDJSON event stream ---
    response = ""
    input_tokens = 0
    output_tokens = 0
    json_parse_error = None
    events: list[dict] = []
    saw_final = False

    try:
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        for event in events:
            event_type = event.get("type")
            if event_type == "final":
                saw_final = True
                response = event.get("text", "") or ""
            elif event_type == "usage":
                usage = event.get("usage", event)
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
            elif event_type == "assistant" and not response:
                # Fall back to an assistant message when the harness did
                # not emit a "final" event.
                message = event.get("message", event)
                content = message.get("content", "")
                if isinstance(content, str):
                    response = content
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                    response = "".join(parts)
    except (json.JSONDecodeError, ValueError) as exc:
        json_parse_error = str(exc)
        if logger:
            logger.log(f"[DSH] JSON parse error: {exc}")

    if not response.strip() and not saw_final and result.stdout.strip():
        # The harness produced no recognisable event at all -- keep the
        # raw output so nothing is lost. A "final" event with an empty
        # body is a genuine failure and must not be masked this way.
        response = result.stdout.strip()

    if result.returncode != 0:
        if logger:
            logger.log(f"[DSH] Non-zero exit code: {result.returncode}")
        if not response.strip():
            if tracker:
                tracker.record(call_name or "dsh", input_tokens, output_tokens,
                               elapsed, provider="dsh", model=model)
            raise ModelRunnerError(
                provider="dsh",
                error_type="non_zero_exit",
                message=f"dsh CLI exited with code {result.returncode}",
                exit_code=result.returncode,
                stderr=result.stderr,
                stdout=result.stdout,
            )

    if not response.strip():
        if logger:
            logger.log("[DSH] Empty response received")
        if tracker:
            tracker.record(call_name or "dsh", input_tokens, output_tokens,
                           elapsed, provider="dsh", model=model)
        raise ModelRunnerError(
            provider="dsh",
            error_type="empty_response",
            message="dsh returned empty response" + (f" (JSON parse error: {json_parse_error})" if json_parse_error else ""),
            exit_code=result.returncode,
            stderr=result.stderr,
            stdout=result.stdout,
        )

    if logger:
        logger.log(f"[DSH] Completed {call_name} in {elapsed:.0f}s "
                   f"({input_tokens} in / {output_tokens} out)")

    if tracker:
        tracker.record(call_name or "dsh", input_tokens, output_tokens,
                       elapsed, provider="dsh", model=model)

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
    if provider not in ("claude", "codex", "gemini", "dsh"):
        raise ValueError(
            f"Unknown provider {provider!r}; expected 'claude', 'codex', "
            f"'gemini', or 'dsh'."
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

    # codex / gemini / dsh: flat dict merge
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
    elif provider == "dsh":
        return await run_dsh_agent(
            prompt, working_dir, config.get("dsh", {}),
            logger=logger, tracker=tracker, call_name=call_name,
        )
    else:
        raise ValueError(f"Unknown model provider: {provider!r}. "
                         f"Expected 'claude', 'codex', 'gemini', or 'dsh'.")


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
