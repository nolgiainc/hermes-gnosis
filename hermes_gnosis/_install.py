"""Installer: copy this plugin into $HERMES_HOME/plugins/gnosis/.

hermes-agent discovers out-of-tree memory providers by scanning
``$HERMES_HOME/plugins/<name>/`` for directories whose ``__init__.py``
implements the MemoryProvider ABC. This console script places the package
there so ``hermes memory setup`` / ``memory.provider: gnosis`` can find it.

Usage:
    pip install hermes-gnosis
    hermes-gnosis-install [--hermes-home PATH]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from ._config import get_hermes_home

_SKIP = {"__pycache__", ".pytest_cache"}


def install(hermes_home: Path) -> Path:
    src = Path(__file__).resolve().parent
    dest = hermes_home / "plugins" / "gnosis"
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in _SKIP or item.suffix == ".pyc":
            continue
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(
                item, target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(item, target)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the gnosis memory plugin into a hermes profile.",
    )
    parser.add_argument(
        "--hermes-home", default=None,
        help="Target HERMES_HOME (default: $HERMES_HOME or ~/.hermes)",
    )
    args = parser.parse_args(argv)

    hermes_home = (
        Path(args.hermes_home).expanduser() if args.hermes_home else get_hermes_home()
    )
    dest = install(hermes_home)
    print(f"Installed gnosis memory plugin -> {dest}")
    print("Activate it with:")
    print("  hermes config set memory.provider gnosis")
    print(f"  echo 'GNOSIS_SERVICE_TOKEN=<token>' >> {hermes_home}/.env")
    print(f"  # and set gnosis_url in {hermes_home}/gnosis.json")
    print("Or run: hermes memory setup   # select 'gnosis'")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
