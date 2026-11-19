"""Entry point: ``labgrid-tui`` or ``python -m labgrid_tui``."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import default_config_path, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="labgrid-tui", description="Terminal UI for labgrid places"
    )
    parser.add_argument("-c", "--config", help=f"config file (default: {default_config_path()})")
    parser.add_argument("-P", "--profile", help="profile name from the config file")
    parser.add_argument(
        "-x", "--coordinator", help="coordinator HOST[:PORT], overrides profile/env"
    )
    parser.add_argument("--list-profiles", action="store_true", help="print profiles and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        if args.profile and args.profile not in config.profiles:
            raise ValueError(
                f"unknown profile '{args.profile}'; known: {', '.join(config.profiles)}"
            )
    except (OSError, ValueError) as e:
        print(f"labgrid-tui: {e}", file=sys.stderr)
        return 2

    if args.list_profiles:
        for name, p in config.profiles.items():
            mark = "*" if name == config.default_profile else " "
            print(f"{mark} {name:15} {p.effective_coordinator()}")
        return 0

    from .app import LabgridTUI  # import late: keeps --help fast

    LabgridTUI(config, profile=args.profile, coordinator_override=args.coordinator).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
