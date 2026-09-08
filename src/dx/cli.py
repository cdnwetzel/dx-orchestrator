import argparse
import sys

from . import __version__
from .cmd_doctor import register_doctor_subcommand
from .cmd_merge import register_merge_subcommand
from .cmd_roles import register_roles_subcommand
from .cmd_run import register_run_subcommand
from .cmd_verify import register_verify_subcommand
from .config_loader import ConfigError
from .ledger_utils import LedgerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dx", description="DevSwarmX control plane")
    parser.add_argument(
        "--version",
        action="version",
        version=f"dx {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_doctor_subcommand(subparsers)
    register_roles_subcommand(subparsers)
    register_run_subcommand(subparsers)
    register_merge_subcommand(subparsers)
    register_verify_subcommand(subparsers)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return

    try:
        args.func(args)
    except (ConfigError, LedgerError) as exc:
        # A malformed manifest or a corrupt ledger is an operator-input problem,
        # not a dx bug. Report it and stop; a traceback here would bury the one
        # line that says what to fix — and dx merge had been printing green
        # checkmarks before crashing on exactly this.
        print(f"❌ {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
