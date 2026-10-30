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
