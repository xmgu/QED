"""Configure a dsh profile for use with the QED pipeline.

The DeepSeek Harness stores its provider routes and credentials per
profile. The ``desktop`` profile -- where the desktop app configures a
third-party OpenAI-compatible endpoint -- is locked to the Electron
application and cannot be used from the command line. The ``headless``
profile, which QED drives, therefore has to carry the same provider
definition itself.

This script copies the relevant entries from the desktop profile into
the headless profile, enables the local credentials plugin (it is
disabled by default, so the CLI cannot read ``~/.dsh/.credentials.yaml``
otherwise), and points the default model at the same route.

Usage::

    python code/setup_dsh_profile.py                 # copy from desktop
    python code/setup_dsh_profile.py --profile web   # copy from another
    python code/setup_dsh_profile.py --show          # print current state
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

import yaml

DSH_HOME = os.path.expanduser(os.path.join("~", ".dsh"))

#: Plugins whose configuration is carried over from the source profile.
#: ``agent-default-model`` selects the provider route and model,
#: ``llm-pi-ai`` defines the routes themselves.
COPIED_PLUGIN_IDS = ("agent-default-model", "llm-pi-ai")

#: The local credentials plugin is disabled in the shipped headless
#: profile, which makes the CLI ignore ~/.dsh/.credentials.yaml.
CREDENTIALS_PLUGIN_ID = "credentials"
CREDENTIALS_PLUGIN_NAME = "@deepseek-ai/dsh-credentials-local"


def profile_dir(name: str) -> str:
    return os.path.join(DSH_HOME, "profiles", name)


def patch_path(profile: str) -> str:
    return os.path.join(profile_dir(profile), "cordis.patch.yml")


def load_patch(profile: str) -> list:
    """Read the patch layer of *profile* as a list of entries."""
    path = patch_path(profile)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    return data if isinstance(data, list) else []


def write_patch(profile: str, entries: list) -> None:
    """Write *entries* to the patch layer of *profile*."""
    path = patch_path(profile)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = path + ".bak"
    if os.path.isfile(path):
        shutil.copyfile(path, backup)
    header = (
        "# Your patch layer for this dsh profile, applied after every bundle\n"
        "# layer. Managed by code/setup_dsh_profile.py -- the previous version\n"
        "# is kept as cordis.patch.yml.bak.\n"
    )
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        yaml.safe_dump(entries, f, allow_unicode=True, sort_keys=False,
                       default_flow_style=False)


def collect(source_entries: list) -> list:
    """Pick the provider-related entries out of *source_entries*."""
    picked = []
    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") in COPIED_PLUGIN_IDS:
            picked.append(entry)
    return picked


# ---------------------------------------------------------------------------
# Credential reference resolution
# ---------------------------------------------------------------------------

CREDENTIALS_FILE = os.path.join(DSH_HOME, ".credentials.yaml")


def credential_refs() -> dict:
    """Return the credential references stored in the credentials service."""
    if not os.path.isfile(CREDENTIALS_FILE):
        return {}
    try:
        with open(CREDENTIALS_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    refs = data.get("refs") or {}
    return refs if isinstance(refs, dict) else {}


def resolve_api_key_env(env_name: str, refs: dict) -> str:
    """Return a reference name the command line can actually resolve.

    The desktop app can point a provider at a *key group* such as
    ``KEYSMGR_SUFE_F3635D``: the ``dsh-keys-manager`` bundle expands it
    into the individual keys it holds. That bundle ships with the
    desktop profile only, so the command line -- which drives the
    headless profile -- cannot resolve a group name and reports
    ``MISSING_CREDENTIAL``.

    The stored references are named after the individual keys
    (``KEYSMGR_SUFE_F3635D_K_<hash>``), plus whatever plain aliases the
    app has written (``SUFE_API_KEY``). So the group name is mapped onto
    a reference that exists.

    The name is returned unchanged when it already resolves.
    """
    if not env_name or env_name in refs:
        return env_name

    # A group name: KEYSMGR_<ROUTE>_F<hash>
    if env_name.startswith("KEYSMGR_"):
        prefix = env_name + "_K_"
        members = [k for k in refs if k.startswith(prefix)]
        if len(members) == 1:
            return members[0]
        if members:
            # Several keys in the group: take the first one in a stable
            # order so repeated runs use the same key.
            return sorted(members)[0]
        # The group is empty -- fall back to a plain alias if there is one.
        aliases = [k for k in refs
                   if k.startswith("KEYSMGR_") and "_K_" not in k]
        if aliases:
            return sorted(aliases)[0]

    return env_name


def group_members(group: str, refs: dict) -> list[str]:
    """Return the individual key references that belong to *group*."""
    prefix = group + "_K_"
    members = sorted(k for k in refs if k.startswith(prefix))
    if members:
        return members
    return [group] if group in refs else []


def probe_key(profile: str, ref_name: str, entries: list) -> str | None:
    """Probe the harness with *ref_name* and report what happened.

    Returns ``"ok"`` when the request was accepted, ``"quota"`` when the
    service refused it for lack of quota, and ``None`` when the probe
    itself could not be carried out.
    """
    import subprocess
    import sys

    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "code"))
    try:
        from compat import resolve_cli
    except ImportError:
        return None

    cli = resolve_cli("dsh")
    if not cli or not os.path.isfile(cli):
        return None

    original = load_patch(profile)
    patched = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == "llm-pi-ai":
            entry = _with_api_key_env(entry, ref_name)
        patched.append(entry)
    write_patch(profile, patched)
    try:
        result = subprocess.run(
            [cli, "--profile", profile, "--json"],
            input="Hi", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  probe failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    finally:
        write_patch(profile, original)

    output = (result.stdout or "") + (result.stderr or "")
    if "insufficient_user_quota" in output:
        return "quota"
    if result.returncode == 0:
        return "ok"
    return None


def _with_api_key_env(entry: dict, ref_name: str) -> dict:
    """Return a copy of *entry* whose routes use *ref_name*."""
    import copy

    clone = copy.deepcopy(entry)
    providers = (clone.get("config", {}) or {}).get("providers")
    if isinstance(providers, dict):
        for spec in providers.values():
            if isinstance(spec, dict):
                spec["apiKeyEnv"] = ref_name
    return clone


def usable_key(profile: str, env_name: str, refs: dict,
               entries: list) -> str:
    """Return the reference of a key that the route can actually use.

    The desktop app rotates through the keys of a group, skipping the
    ones that ran out of quota. The command line has no key manager, so
    the keys are probed one by one here: a request is attempted with
    each candidate until one is accepted.

    The first candidate is returned when none can be verified -- the
    harness then reports the underlying error, which is more useful than
    a probe that hides it.
    """
    candidates = group_members(env_name, refs) or [env_name]
    if len(candidates) == 1:
        return candidates[0]

    for candidate in candidates:
        print(f"  trying {candidate} ...", flush=True)
        outcome = probe_key(profile, candidate, entries)
        if outcome == "ok":
            print(f"  {candidate} works", flush=True)
            return candidate
        if outcome == "quota":
            print(f"  {candidate} is out of quota", flush=True)
        else:
            print(f"  {candidate} could not be probed", flush=True)
    return candidates[0]


def build_credentials_entry() -> dict:
    """Return the entry that enables the local credentials plugin."""
    return {
        "id": CREDENTIALS_PLUGIN_ID,
        "name": CREDENTIALS_PLUGIN_NAME,
        "disabled": False,
    }


def show(profile: str) -> None:
    """Print the provider configuration of *profile*."""
    print(f"=== profile: {profile} ({patch_path(profile)}) ===")
    entries = load_patch(profile)
    if not entries:
        print("  (empty patch layer)")
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("id")
        if pid == "agent-default-model":
            cfg = entry.get("config", {}) or {}
            print(f"  agent-default-model: provider={cfg.get('provider')!r} "
                  f"model={cfg.get('model')!r}")
        elif pid == CREDENTIALS_PLUGIN_ID:
            print(f"  credentials: disabled={entry.get('disabled')}")
        elif pid == "llm-pi-ai":
            providers = (entry.get("config", {}) or {}).get("providers", {}) or {}
            for name, spec in providers.items():
                models = [m.get("id") for m in spec.get("models", []) or []]
                print(f"  route {name!r}: baseURL={spec.get('baseURL')!r} "
                      f"apiKeyEnv={spec.get('apiKeyEnv')!r} models={models}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="desktop",
                        help="profile to copy the provider config from")
    parser.add_argument("--profile", default="headless",
                        help="profile to write the provider config to")
    parser.add_argument("--show", action="store_true",
                        help="print the current state and exit")
    args = parser.parse_args()

    if args.show:
        show(args.source)
        print()
        show(args.profile)
        return 0

    source_entries = load_patch(args.source)
    if not source_entries:
        print(f"No patch entries found in profile {args.source!r}. "
              f"Configure the provider in the desktop app first.")
        return 1

    picked = collect(source_entries)
    if not picked:
        print(f"Profile {args.source!r} has no provider configuration "
              f"(looked for {', '.join(COPIED_PLUGIN_IDS)}).")
        return 1

    # The desktop app may point a provider at a key group that only its
    # own keys-manager bundle can expand. Map it onto a reference the
    # command line can resolve, and pick a key that still has quota.
    refs = credential_refs()

    # The entries that will end up in the target profile -- the probe
    # needs the complete set, because a profile without the credentials
    # plugin cannot authenticate at all.
    target_entries = load_patch(args.profile)
    kept = [e for e in target_entries
            if not (isinstance(e, dict) and e.get("id") in COPIED_PLUGIN_IDS)]
    kept = [e for e in kept
            if not (isinstance(e, dict) and e.get("id") == CREDENTIALS_PLUGIN_ID)]
    probe_entries = picked + [build_credentials_entry()] + kept

    rewritten = []
    for entry in picked:
        if entry.get("id") == "llm-pi-ai":
            providers = (entry.get("config", {}) or {}).get("providers")
            if isinstance(providers, dict):
                for spec in providers.values():
                    if not isinstance(spec, dict):
                        continue
                    env_name = spec.get("apiKeyEnv")
                    if not env_name:
                        continue
                    resolved = resolve_api_key_env(env_name, refs)
                    if resolved != env_name:
                        print(f"apiKeyEnv {env_name!r} -> {resolved!r} "
                              f"(the group name is only resolvable by the "
                              f"desktop app's key manager)")
                        spec["apiKeyEnv"] = resolved
                    # The group holds several keys and the desktop app
                    # rotates through them; pick one that still works.
                    if len(group_members(env_name, refs)) > 1:
                        chosen = usable_key(args.profile, env_name, refs,
                                            probe_entries)
                        if chosen != spec.get("apiKeyEnv"):
                            print(f"using key {chosen!r} "
                                  f"(the others are out of quota)")
                            spec["apiKeyEnv"] = chosen
        rewritten.append(entry)
    picked = rewritten

    target_entries = load_patch(args.profile)
    # Drop the entries we are about to replace, keep everything else.
    kept = [e for e in target_entries
             if not (isinstance(e, dict) and e.get("id") in COPIED_PLUGIN_IDS)]
    kept = [e for e in kept
            if not (isinstance(e, dict) and e.get("id") == CREDENTIALS_PLUGIN_ID)]

    result = picked + [build_credentials_entry()] + kept
    write_patch(args.profile, result)

    print(f"Profile {args.profile!r} updated from {args.source!r}.")
    print(f"  backup: {patch_path(args.profile)}.bak")
    print()
    show(args.profile)
    print()
    print("Verify with:")
    print(f"  echo 'Reply with exactly: OK' | dsh --profile {args.profile} --json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
