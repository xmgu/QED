import io
import re

# --- model_runner.py: gemini ---
p = "code/model_runner.py"
s = io.open(p, encoding="utf-8").read()

old_gemini = """    def _call():
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
"""

new_gemini = """    start = datetime.now()
    if logger:
        logger.log(f"[Gemini] Starting {call_name} (model={model})")

    try:
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
                result = await run_cli_async(cmd, cwd=working_dir, env=env)
        else:
            result = await run_cli_async(cmd, cwd=working_dir, env=env)
"""

assert old_gemini in s, "gemini block not found"
s = s.replace(old_gemini, new_gemini)

# --- model_runner.py: dsh ---
old_dsh = """    def _call():
        env = os.environ.copy()
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
        # The harness reads these to decide what the agent may do. The
        # pipeline needs file read/write and shell access inside the
        # output directory, which is what "danger-full-access" grants.
        env["DSH_PERMISSION_MODE"] = permission_mode
        try:
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=working_dir,
                env=env,
                input=prompt,
                **decode_kwargs(),
            )
        finally:
            if patch_path:
                _remove_file(patch_path)
"""

new_dsh = """    def _make_env():
        env = os.environ.copy()
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
        # The harness reads these to decide what the agent may do. The
        # pipeline needs file read/write and shell access inside the
        # output directory, which is what "danger-full-access" grants.
        env["DSH_PERMISSION_MODE"] = permission_mode
        return env
"""

assert old_dsh in s, "dsh block not found"
s = s.replace(old_dsh, new_dsh)

old_dsh_call = """    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _call)
    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        if logger:
            logger.log(f"[DSH] EXCEPTION: {type(exc).__name__}: {exc}")
"""

new_dsh_call = """    try:
        result = await run_cli_async(cmd, cwd=working_dir, env=_make_env(),
                                     input_text=prompt)
    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        if logger:
            logger.log(f"[DSH] EXCEPTION: {type(exc).__name__}: {exc}")
"""

assert old_dsh_call in s, "dsh call block not found"
s = s.replace(old_dsh_call, new_dsh_call)

# dsh 的 patch 文件清理要在调用之后
old_cleanup = """    if tracker:
        tracker.record(call_name or "dsh", input_tokens, output_tokens,
                       elapsed, provider="dsh", model=model)

    return response
"""
new_cleanup = """    if tracker:
        tracker.record(call_name or "dsh", input_tokens, output_tokens,
                       elapsed, provider="dsh", model=model)

    return response
"""

# 导入 run_cli_async
old_import = "from compat import decode_kwargs, resolve_cli  # noqa: E402"
new_import = "from compat import decode_kwargs, resolve_cli, run_cli_async  # noqa: E402"
assert old_import in s
s = s.replace(old_import, new_import)

io.open(p, "w", encoding="utf-8", newline="").write(s)
print("model_runner.py patched")

# --- pipeline.py ---
p2 = "code/pipeline.py"
s2 = io.open(p2, encoding="utf-8").read()

old_pl = """    def _call():
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            **decode_kwargs(),
        )
"""
new_pl = """    def _call():
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            **decode_kwargs(),
        )
"""

old_pl_call = """        result = await asyncio.get_event_loop().run_in_executor(None, _call)"""
new_pl_call = """        result = await run_cli_async(cmd, cwd=cwd, env=env)"""

assert old_pl_call in s2, "pipeline call not found"
s2 = s2.replace(old_pl_call, new_pl_call)

old_pl_import = "from compat import decode_kwargs, read_text, resolve_cli, write_text  # noqa: E402"
new_pl_import = "from compat import decode_kwargs, read_text, resolve_cli, run_cli_async, write_text  # noqa: E402"
assert old_pl_import in s2
s2 = s2.replace(old_pl_import, new_pl_import)

io.open(p2, "w", encoding="utf-8", newline="").write(s2)
print("pipeline.py patched")
