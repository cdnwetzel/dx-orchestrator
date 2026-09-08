"""CLI surface: version reporting and subcommand registration."""
import tomllib
from pathlib import Path

import pytest

import dx
from dx.cli import build_parser

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_version_flag_reports_the_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"dx {dx.__version__}"


def test_package_version_matches_pyproject():
    """Two declared versions must not drift apart."""
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert declared == dx.__version__


def test_all_subcommands_are_registered():
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    registered = set(actions[0].choices)
    assert registered == {"doctor", "roles", "run", "merge", "verify-gui"}


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor"],
        ["doctor", "--no-network"],
        ["roles", "list"],
        ["roles", "list", "--fit", "High"],
        ["roles", "validate"],
        ["run", "T-1", "-m", "x"],
        ["run", "T-1", "-m", "x", "--dry-run", "--force"],
        ["merge", "T-1"],
        ["merge", "T-1", "--force", "--verify-gui"],
        ["verify-gui"],
        ["verify-gui", "--json"],
    ],
)
def test_documented_invocations_parse(argv):
    args = build_parser().parse_args(argv)
    assert hasattr(args, "func")


def test_run_requires_a_message():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "T-1"])


def test_roles_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["roles"])
