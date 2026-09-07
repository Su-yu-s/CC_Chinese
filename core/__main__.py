"""Launcher for elevated subprocesses.

When launched as an elevated subprocess, this module ensures the project root
is on sys.path so that `import core.elevation` works regardless of cwd.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from main import _handle_elevation_cli, _run_self_test  # noqa: E402

# Re-export main's entry points for use by the elevated broker
def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv
    result = _handle_elevation_cli(args)
    if result is not None:
        return result
    result = _run_self_test(args)
    if result is not None:
        return result
    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
