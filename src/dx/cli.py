import argparse

from . import __version__
from .cmd_doctor import register_doctor_subcommand
from .cmd_merge import register_merge_subcommand
from .cmd_roles import register_roles_subcommand
from .cmd_run import register_run_subcommand
from .cmd_verify import register_verify_subcommand


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
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
