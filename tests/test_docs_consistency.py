"""Documentation that cannot silently rot.

A README is a claim like any other. These tests hold the ones that are
mechanically checkable: version numbers agreeing across three files, the
environment overrides being documented exactly as implemented, and every CLI
subcommand appearing in the command table.

This is deliberately narrow. It checks facts a machine can verify, not prose
quality — and it exists because the docs already drifted once: `dx --version`
did not exist while `__version__` did, and the roles path was documented as
overridable months before it was.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

import dx
from dx.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "dx"
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class TestVersion:
    def test_pyproject_matches_package(self):
        assert PYPROJECT["project"]["version"] == dx.__version__

    def test_changelog_has_an_entry_for_the_current_version(self):
        assert f"## [{dx.__version__}]" in CHANGELOG, (
            f"CHANGELOG.md has no section for {dx.__version__}"
        )

    def test_changelog_newest_entry_is_the_current_version(self):
        versions = re.findall(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", CHANGELOG, re.MULTILINE)
        assert versions, "no released versions found in CHANGELOG.md"
        assert versions[0] == dx.__version__, (
            f"newest CHANGELOG entry is {versions[0]}, package is {dx.__version__}"
        )

    def test_every_changelog_version_has_a_link_definition(self):
        for version in re.findall(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", CHANGELOG, re.MULTILINE):
            assert f"[{version}]: https://" in CHANGELOG, f"no link target for {version}"


class TestEnvironmentOverrides:
    """Every override the code reads must be documented, and vice versa.

    An undocumented override is a feature nobody can use; a documented one that
    does not exist is a lie that costs somebody an afternoon.
    """

    # Read from the environment but intentionally absent from the README table:
    # PXX_* are set by dx for the pxx subprocess, not read from the user.
    NOT_USER_FACING = {"PXX_BASE_URL", "PXX_MODEL", "PXX_PROVIDER", "VIRTUAL_ENV"}

    @staticmethod
    def _env_vars_read_in_source() -> set[str]:
        """Any DX_/PSOPERATOR_ name appearing as a string literal in the package.

        Matching only ``os.environ.get("LITERAL")`` missed the names dx passes
        through a helper parameter, so the check silently under-reported.
        """
        found: set[str] = set()
        for path in SRC.glob("*.py"):
            found |= set(
                re.findall(r"[\"']((?:DX|PSOPERATOR|PXX)_[A-Z0-9_]+)[\"']", path.read_text())
            )
        return found

    @staticmethod
    def _env_vars_documented() -> set[str]:
        table = README.split("### Environment overrides", 1)
        assert len(table) == 2, "README has no 'Environment overrides' section"
        body = table[1].split("\n## ", 1)[0]
        return set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", body))

    def test_every_override_read_by_dx_is_documented(self):
        undocumented = (
            self._env_vars_read_in_source()
            - self._env_vars_documented()
            - self.NOT_USER_FACING
        )
        assert not undocumented, (
            f"read by dx but missing from the README table: {sorted(undocumented)}"
        )

    def test_every_documented_override_is_actually_read(self):
        phantom = self._env_vars_documented() - self._env_vars_read_in_source()
        assert not phantom, (
            f"documented in the README but never read by dx: {sorted(phantom)}"
        )

    def test_the_check_is_not_vacuous(self):
        """Guard against both sets being empty and the tests above passing for
        the wrong reason."""
        documented = self._env_vars_documented()
        assert "DX_CONFIG" in documented
        assert "DX_ROLES_PATH" in documented
        assert len(documented) >= 5


class TestCommandTable:
    @staticmethod
    def _subcommands() -> set[str]:
        parser = build_parser()
        actions = [a for a in parser._actions if getattr(a, "choices", None)]
        return set(actions[0].choices)

    def test_every_subcommand_appears_in_the_readme_table(self):
        table = README.split("## Commands", 1)[1].split("\n## ", 1)[0]
        for command in self._subcommands():
            assert f"`dx {command}" in table, f"`dx {command}` is not in the command table"

    def test_documented_exit_codes_match_the_anchored_block(self):
        """README promises exit 2 specifically for a refused Anchored role."""
        assert "`2` Anchored role refused" in README


class TestClaimsAreQualified:
    """The README makes two load-bearing claims about the suite. If either stops
    being true, the sentence has to change with it."""

    def test_hermetic_claim_holds(self):
        """No test may depend on a private sibling repo without a skip guard.

        Keyed on the actual clone paths rather than the repo names, so a test
        that merely asserts on an error message mentioning devswarm-ledger is
        not flagged.
        """
        assert "hermetic" in README
        # Match the construct that creates a real dependency — a clone path
        # turned into a Path — rather than any mention of one. A docstring
        # explaining a past bug is not a dependency.
        dependency = re.compile(r"""Path\(\s*["']~/ai/""")
        for path in (ROOT / "tests").glob("test_*.py"):
            text = path.read_text()
            if dependency.search(text):
                assert "skipif" in text, (
                    f"{path.name} depends on a private clone without a skip guard"
                )

    def test_the_hermetic_guard_actually_fires(self):
        """The guard is worthless if its pattern matches nothing; the real-card
        tests are the one place a clone path is legitimately used."""
        dependency = re.compile(r"""Path\(\s*["']~/ai/""")
        guarded = [
            path.name
            for path in (ROOT / "tests").glob("test_*.py")
            if dependency.search(path.read_text())
        ]
        assert guarded, "hermetic guard matched no files — pattern is probably wrong"

    def test_no_remote_host_defaults_claim_holds(self):
        """README: 'There are no hardcoded remote hosts anywhere in the package.'"""
        assert "no hardcoded remote hosts" in README
        ip_literal = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for path in SRC.glob("*.py"):
            for line in path.read_text().splitlines():
                for match in ip_literal.findall(line):
                    assert match.startswith("127."), (
                        f"{path.name}: hardcoded address {match!r} in {line.strip()!r}"
                    )


@pytest.mark.parametrize(
    "doc",
    [
        "README.md", "VISION.md", "RESOURCES.md", "CHANGELOG.md",
        "TUTORIAL.md", "checkpoint.md", "SECURITY.md", "CONTRIBUTING.md",
        "LICENSE",
    ],
)
def test_documented_files_exist(doc):
    assert (ROOT / doc).is_file(), f"{doc} is referenced by the docs index but missing"


TUTORIAL = (ROOT / "TUTORIAL.md").read_text(encoding="utf-8")
REAL_CARDS = Path("~/ai/claude-sdlc-roles/skills/sdlc-role/roles").expanduser()


class TestTutorialFidelity:
    """TUTORIAL.md stakes its worth on one claim: every output shown was
    captured from a real session. That claim decays silently.

    It had already: the `dx roles list --fit High` block showed column widths
    that command cannot produce (widths are sized from the filtered data), so it
    predated the hardcoded-width fix and had never been recaptured. A promise of
    fidelity that nothing checks is just a promise.
    """

    def test_the_version_shown_matches_the_package(self):
        assert f"dx --version    # dx {dx.__version__}" in TUTORIAL, (
            "the `dx --version` comment in TUTORIAL.md §2 is stale"
        )

    def test_it_declares_its_one_edit(self):
        """Host addresses are substituted; that substitution must stay declared,
        because an undeclared edit would cost more than the addresses did."""
        assert "One declared edit" in TUTORIAL
        assert "no output was reworded, reordered, or invented" in TUTORIAL

    def test_no_real_addresses_leaked_back_in(self):
        assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", TUTORIAL), (
            "a raw IP address is in TUTORIAL.md; use the .lab placeholders"
        )

    def test_it_still_states_what_it_does_not_establish(self):
        """The closing boundary paragraph is the honesty contract."""
        assert "does **not** establish" in TUTORIAL

    @pytest.mark.skipif(
        not REAL_CARDS.is_dir(),
        reason="claude-sdlc-roles not cloned (private repo; skipped in CI)",
    )
    def test_the_roles_list_transcript_is_reproducible(self):
        """Run the command the tutorial shows and require the shown output."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "dx.cli", "roles", "list", "--fit", "High"],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(Path.home()),
                "PYTHONPATH": str(ROOT / "src"),
            },
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip("\n") in TUTORIAL, (
            "the `dx roles list --fit High` transcript in TUTORIAL.md no longer "
            "matches what that command prints:\n" + result.stdout
        )


class TestTestCountClaim:
    """The README cites a specific test count. That number is persuasive to a
    reviewer, which is exactly why it must not be allowed to drift — so it is
    checked against the real collected count rather than trusted.
    """

    @staticmethod
    def _claimed() -> int:
        match = re.search(r"(\d[\d,]*)\s+tests,\s*\n?including real-GPG", README)
        if match is None:
            match = re.search(r"(\d[\d,]*)\s+tests", README)
        assert match, "README no longer states a test count"
        return int(match.group(1).replace(",", ""))

    def test_the_readme_count_matches_the_suite(self, request):
        collected = len(request.session.items)
        # `pytest -k` / single-file runs collect a subset; only assert on a full run.
        if collected < 50:
            pytest.skip("partial collection — only meaningful on a full run")
        claimed = self._claimed()
        assert claimed == collected, (
            f"README claims {claimed} tests, the suite collects {collected}"
        )
