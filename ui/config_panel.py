"""Sidebar configuration panel for decomposition-mode runs."""

import copy
import os

import streamlit as st

from utils import (
    load_config,
    ORIGINAL_CONFIG_PATH,
    MODEL_PROVIDERS,
    AGENT_NAMES,
    PIPELINE_AGENT_NAMES,
    CODEX_REASONING_LEVELS,
    GEMINI_THINKING_LEVELS,
    DSH_PERMISSION_MODES,
)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _init_config() -> dict:
    """Load config.yaml once and cache in session state."""
    if "config_disk" not in st.session_state:
        try:
            st.session_state.config_disk = load_config(ORIGINAL_CONFIG_PATH)
        except Exception:
            st.session_state.config_disk = {}
    return st.session_state.config_disk


# ---------------------------------------------------------------------------
# Global provider blocks (adapted from archived config_panel.py)
# ---------------------------------------------------------------------------

def _render_claude_block(claude_cfg: dict) -> dict:
    st.sidebar.subheader("Claude (Anthropic)")
    sub_cfg = claude_cfg.get("subscription", {})
    bed_cfg = claude_cfg.get("bedrock", {})
    api_cfg = claude_cfg.get("api_key", {})

    provider_options = ["subscription", "bedrock", "api_key"]
    current_provider = claude_cfg.get("provider", "api_key")
    if current_provider not in provider_options:
        current_provider = "api_key"
    claude_provider = st.sidebar.selectbox(
        "Authentication",
        provider_options,
        index=provider_options.index(current_provider),
        key="claude_provider",
    )

    sub_model = sub_cfg.get("model", "claude-fable-5-1")
    bed_model = bed_cfg.get("model", "anthropic.claude-fable-5-1")
    bed_profile = bed_cfg.get("aws_profile", "default")
    api_model = api_cfg.get("model", "claude-fable-5-1")
    api_key_val = api_cfg.get("key", "")

    if claude_provider == "subscription":
        sub_model = st.sidebar.text_input(
            "Model", value=sub_model, key="claude_sub_model",
        )
    elif claude_provider == "bedrock":
        bed_model = st.sidebar.text_input(
            "Bedrock model ID", value=bed_model, key="claude_bed_model",
        )
        bed_profile = st.sidebar.text_input(
            "AWS profile", value=bed_profile, key="claude_bed_profile",
        )
    else:
        api_model = st.sidebar.text_input(
            "API model", value=api_model, key="claude_api_model",
        )
        api_key_val = st.sidebar.text_input(
            "Anthropic API key", value=api_key_val, type="password",
            key="claude_api_key",
        )

    with st.sidebar.expander("Claude Advanced"):
        cli_path = st.text_input(
            "CLI path", value=claude_cfg.get("cli_path", "claude"),
            key="claude_cli",
        )
        perm_mode = st.text_input(
            "Permission mode",
            value=claude_cfg.get("permission_mode", "bypassPermissions"),
            key="claude_perm",
        )

    return {
        **claude_cfg,
        "cli_path": cli_path,
        "permission_mode": perm_mode,
        "provider": claude_provider,
        "subscription": {**sub_cfg, "model": sub_model},
        "bedrock": {**bed_cfg, "model": bed_model, "aws_profile": bed_profile},
        "api_key": {**api_cfg, "model": api_model, "key": api_key_val},
    }


def _render_codex_block(codex_cfg: dict) -> dict:
    st.sidebar.subheader("Codex (OpenAI)")
    codex_model = st.sidebar.text_input(
        "Model", value=codex_cfg.get("model", "gpt-6-astra"),
        key="codex_model",
    )
    cur_effort = codex_cfg.get("reasoning_effort", "max")
    if cur_effort not in CODEX_REASONING_LEVELS:
        cur_effort = "max"
    codex_effort = st.sidebar.selectbox(
        "Reasoning effort", list(CODEX_REASONING_LEVELS),
        index=CODEX_REASONING_LEVELS.index(cur_effort),
        key="codex_effort",
    )
    with st.sidebar.expander("Codex Advanced"):
        codex_cli = st.text_input(
            "CLI path", value=codex_cfg.get("cli_path", "codex"),
            key="codex_cli",
        )
    return {
        **codex_cfg,
        "cli_path": codex_cli,
        "model": codex_model,
        "reasoning_effort": codex_effort,
    }


def _render_gemini_block(gemini_cfg: dict) -> dict:
    st.sidebar.subheader("Gemini (Google)")
    gemini_model = st.sidebar.text_input(
        "Model", value=gemini_cfg.get("model", "gemini-3.1-pro-preview"),
        key="gemini_model",
    )
    thinking_options = ("", *GEMINI_THINKING_LEVELS)
    cur_think = gemini_cfg.get(
        "thinking_level", "" if gemini_cfg.get("thinking_budget") is not None else "HIGH",
    )
    if cur_think not in thinking_options:
        cur_think = "HIGH"
    gemini_think = st.sidebar.selectbox(
        "Thinking level", list(thinking_options),
        index=thinking_options.index(cur_think),
        format_func=lambda level: level or "(model default)",
        key="gemini_think",
    )
    gemini_api = st.sidebar.text_input(
        "Gemini API key", value=gemini_cfg.get("api_key", ""),
        type="password", key="gemini_api",
    )
    with st.sidebar.expander("Gemini Advanced"):
        gemini_cli = st.text_input(
            "CLI path", value=gemini_cfg.get("cli_path", "gemini"),
            key="gemini_cli",
        )
        gemini_approval = st.text_input(
            "Approval mode",
            value=gemini_cfg.get("approval_mode", "yolo"),
            key="gemini_approval",
        )
    return {
        **gemini_cfg,
        "cli_path": gemini_cli,
        "model": gemini_model,
        "approval_mode": gemini_approval,
        "thinking_level": gemini_think,
        "api_key": gemini_api,
    }


def _render_dsh_block(dsh_cfg: dict) -> dict:
    st.sidebar.subheader("DeepSeek Harness (dsh)")
    dsh_model = st.sidebar.text_input(
        "Model", value=dsh_cfg.get("model", "deepseek-flash"),
        key="dsh_model",
    )
    dsh_api = st.sidebar.text_input(
        "DeepSeek API key", value=dsh_cfg.get("api_key", ""),
        type="password", key="dsh_api",
    )
    with st.sidebar.expander("dsh Advanced"):
        dsh_cli = st.text_input(
            "CLI path", value=dsh_cfg.get("cli_path", "dsh"),
            key="dsh_cli",
        )
        dsh_profile = st.text_input(
            "Profile", value=dsh_cfg.get("profile", "headless"),
            key="dsh_profile",
        )
        dsh_permission = st.selectbox(
            "Permission mode",
            list(DSH_PERMISSION_MODES),
            index=DSH_PERMISSION_MODES.index(
                dsh_cfg.get("permission_mode", "danger-full-access")
                if dsh_cfg.get("permission_mode", "danger-full-access")
                in DSH_PERMISSION_MODES else "danger-full-access"
            ),
            key="dsh_permission",
        )
    return {
        **dsh_cfg,
        "cli_path": dsh_cli,
        "profile": dsh_profile,
        "model": dsh_model,
        "permission_mode": dsh_permission,
        "api_key": dsh_api,
    }


# ---------------------------------------------------------------------------
# Per-agent override blocks
# ---------------------------------------------------------------------------

_USE_GLOBAL = "(use global)"


def _agent_block(name: str, agent_cfg: dict) -> dict:
    """Render the per-agent override widgets for one agent.

    Returns a dict with at least ``provider`` and the optional ``model``,
    ``reasoning_effort``, ``thinking_level``, ``thinking_budget`` fields
    when the user has set them.
    """
    container = st.container(border=True)
    with container:
        st.markdown(f"**{name.replace('_', ' ').title()}**")
        cur_provider = agent_cfg.get("provider", "codex")
        if cur_provider not in MODEL_PROVIDERS:
            cur_provider = "codex"
        provider = st.selectbox(
            "Provider",
            list(MODEL_PROVIDERS),
            index=MODEL_PROVIDERS.index(cur_provider),
            key=f"agent_{name}_provider",
        )
        model_default = agent_cfg.get("model", "")
        model_value = st.text_input(
            "Model override (blank = use global)",
            value=model_default,
            key=f"agent_{name}_model",
        )
        reasoning_value = None
        thinking_value = None
        thinking_budget = None
        if provider == "codex":
            opts = [_USE_GLOBAL, *CODEX_REASONING_LEVELS]
            cur = agent_cfg.get("reasoning_effort", "")
            if cur not in CODEX_REASONING_LEVELS:
                idx = 0
            else:
                idx = 1 + CODEX_REASONING_LEVELS.index(cur)
            reasoning_value = st.selectbox(
                "Reasoning effort",
                opts, index=idx,
                key=f"agent_{name}_reasoning",
            )
        elif provider == "gemini":
            opts = [_USE_GLOBAL, *GEMINI_THINKING_LEVELS]
            cur = agent_cfg.get("thinking_level", "")
            if cur not in GEMINI_THINKING_LEVELS:
                idx = 0
            else:
                idx = 1 + GEMINI_THINKING_LEVELS.index(cur)
            thinking_value = st.selectbox(
                "Thinking level",
                opts, index=idx,
                key=f"agent_{name}_thinking",
            )
            cur_budget = agent_cfg.get("thinking_budget", 0)
            thinking_budget = st.number_input(
                "Thinking budget (0 = use global)",
                min_value=0, max_value=200_000,
                value=int(cur_budget) if isinstance(cur_budget, int) else 0,
                step=1024,
                key=f"agent_{name}_thinking_budget",
            )

    out = {k: v for k, v in agent_cfg.items()
           if k not in ("provider", "model", "reasoning_effort",
                        "thinking_level", "thinking_budget")}
    out["provider"] = provider
    if model_value.strip():
        out["model"] = model_value.strip()
    if reasoning_value and reasoning_value != _USE_GLOBAL:
        out["reasoning_effort"] = reasoning_value
    if thinking_value and thinking_value != _USE_GLOBAL:
        out["thinking_level"] = thinking_value
    if thinking_budget:
        out["thinking_budget"] = int(thinking_budget)
    return out


# ---------------------------------------------------------------------------
# Shell status
# ---------------------------------------------------------------------------

def _render_shell_status() -> None:
    """Show which shell will run ``run.sh`` on Windows.

    The pipeline entry point is a shell script. On Windows the launcher
    must be a real Git Bash / MSYS2 shell -- the ``bash.exe`` in
    ``C:\\Windows\\System32`` is the WSL stub and cannot execute the
    project's scripts. Surfacing the resolved shell turns a cryptic
    "No such file or directory" into an actionable message.
    """
    if os.name != "nt":
        return

    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "code"))
    try:
        from compat import describe_bash, find_bash
        bash = find_bash()
        description = describe_bash(bash)
    except Exception:
        return

    with st.sidebar.expander("Shell", expanded=False):
        st.caption("Shell used to launch `run.sh`:")
        st.code(description, language="text")
        if bash is None:
            st.error(
                "No usable bash found. Install Git for Windows, or set the "
                "QED_BASH environment variable to the full path of bash.exe."
            )
        elif "WSL" in description:
            st.error(
                "The resolved bash is the WSL launcher, which cannot run "
                "this project's scripts. Set QED_BASH to a Git Bash binary."
            )
        else:
            st.success("Shell is ready.")


# ---------------------------------------------------------------------------
# Main panel
# ---------------------------------------------------------------------------

def render_config_panel() -> dict:
    """Render the sidebar and return the full assembled config dict."""
    cfg = _init_config()

    st.sidebar.header("Global Provider Defaults")
    claude_block = _render_claude_block(cfg.get("claude", {}))
    codex_block = _render_codex_block(cfg.get("codex", {}))
    gemini_block = _render_gemini_block(cfg.get("gemini", {}))
    dsh_block = _render_dsh_block(cfg.get("dsh", {}))

    st.sidebar.caption("Prover mode: decomposition (only mode supported)")

    # --- Shell used to launch the pipeline ---
    _render_shell_status()

    # --- Decomposition agents ---
    decomp_cfg = cfg.get("decomposition", {})
    models_cfg = decomp_cfg.get("models", {})

    with st.sidebar.expander("Decomposition Agents", expanded=False):
        max_proof_attempts = st.number_input(
            "max_proof_attempts (REVISE_PROOF)",
            min_value=1, max_value=20,
            value=int(decomp_cfg.get("max_proof_attempts", 4)),
            key="decomp_max_proof_attempts",
        )
        max_revisions = st.number_input(
            "max_revisions (REVISE_PLAN)",
            min_value=1, max_value=20,
            value=int(decomp_cfg.get("max_revisions", 4)),
            key="decomp_max_revisions",
        )
        max_decompositions = st.number_input(
            "max_decompositions (REWRITE)",
            min_value=1, max_value=20,
            value=int(decomp_cfg.get("max_decompositions", 4)),
            key="decomp_max_decompositions",
        )
        decomp_agents = {
            name: _agent_block(name, models_cfg.get(name, {}))
            for name in AGENT_NAMES
        }

    # --- Pipeline (Stage 0 + Stage 2) agents ---
    pipeline_cfg = cfg.get("pipeline", {})
    with st.sidebar.expander("Pipeline Agents (Stage 0 & 2)", expanded=False):
        pipeline_agents = {
            name: _agent_block(name, pipeline_cfg.get(name, {}))
            for name in PIPELINE_AGENT_NAMES
        }

    # --- Assemble final dict; preserve unknown top-level keys ---
    result: dict = copy.deepcopy(cfg) if cfg else {}
    result["claude"] = claude_block
    result["codex"] = codex_block
    result["gemini"] = gemini_block
    result["dsh"] = dsh_block
    result["prover"] = {"mode": "decomposition"}
    result["pipeline"] = {
        **{name: pipeline_agents[name] for name in PIPELINE_AGENT_NAMES},
    }
    # Preserve any other keys that were originally under pipeline.
    for k, v in pipeline_cfg.items():
        if k not in PIPELINE_AGENT_NAMES:
            result["pipeline"][k] = v
    result["decomposition"] = {
        "max_proof_attempts": int(max_proof_attempts),
        "max_revisions": int(max_revisions),
        "max_decompositions": int(max_decompositions),
        "models": decomp_agents,
    }
    # Preserve other top-level decomposition keys (rare but defensive).
    for k, v in decomp_cfg.items():
        if k not in ("max_proof_attempts", "max_revisions",
                     "max_decompositions", "models"):
            result["decomposition"][k] = v

    return result
