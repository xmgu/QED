"""Remove the generated output directories.

Deleting the whole output directory by hand is easy to get wrong -- a
typo in the path removes something that was not meant to be removed.
This script only ever removes the directories it created, and it
refuses to touch anything else.
"""

from __future__ import annotations

import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The directories the pipeline creates.
TARGETS = ("proof_output", "standalone_verifier_result")


def main() -> int:
    for name in TARGETS:
        path = os.path.join(PROJECT_ROOT, name)
        if not os.path.isdir(path):
            print(f"  (nothing to remove: {name})")
            continue
        # A guard against a stray symlink pointing somewhere else.
        if os.path.islink(path):
            print(f"  skipping {name}: it is a link")
            continue
        shutil.rmtree(path, ignore_errors=True)
        print(f"  removed {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
