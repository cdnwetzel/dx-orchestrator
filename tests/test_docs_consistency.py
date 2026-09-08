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
        """No test may require a sibling clone without a skip guard.

        The sibling repos are public now, so this is no longer about privacy —
        it is about the README's claim that `pytest` runs with nothing but this
        repository. A test that hard-requires a clone breaks that claim whether
        or not the clone is reachable.

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
                    f"{path.name} requires a sibling clone without a skip guard"
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
REAL_CARDS = Path("~/ai/sdlc-agent-roles/skills/sdlc-role/roles").expanduser()


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
        reason="sdlc-agent-roles not cloned; CI clones it, so this should run there",
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
        # A narrowed invocation collects a subset, so the count claim is only
        # meaningful on a full run. Keyed on the *invocation* rather than a
        # threshold on the collected count: the old `< 50` guard let a two-file
        # run through at 51 collected and asserted 327 == 51. A magic number
        # that happens to sit just below a plausible invocation is not a guard.
        opts = request.config.option
        narrowed = bool(
            getattr(opts, "keyword", "")
            or getattr(opts, "markexpr", "")
            or request.config.args != ["tests"]   # pyproject testpaths
        )
        if narrowed:
            pytest.skip("narrowed invocation — the count claim needs a full run")
        collected = len(request.session.items)
        claimed = self._claimed()
        assert claimed == collected, (
            f"README claims {claimed} tests, the suite collects {collected}"
        )


class TestExitCodeContract:
    """The README's exit-code line is the CLI's published contract. It drifted
    once already: `dx run` returned pxx's exit code verbatim, so an undocumented
    `3` (and a `2` meaning something entirely different) could reach a caller."""

    @staticmethod
    def _documented() -> set[int]:
        line = next(
            (ln for ln in README.splitlines() if ln.startswith("Exit codes:")), ""
        )
        assert line, "README no longer states exit codes"
        return {int(m) for m in re.findall(r"`(\d+)`", line)}

    def test_every_defined_code_is_documented(self):
        from dx import cmd_run

        defined = {
            v for k, v in vars(cmd_run).items()
            if k.startswith("EXIT_") and isinstance(v, int)
        }
        undocumented = defined - self._documented()
        assert not undocumented, (
            f"cmd_run defines exit code(s) {sorted(undocumented)} the README does not "
            "document"
        )

    def test_no_documented_code_is_imaginary(self):
        from dx import cmd_run

        defined = {
            v for k, v in vars(cmd_run).items()
            if k.startswith("EXIT_") and isinstance(v, int)
        }
        assert self._documented() <= defined, (
            "the README documents an exit code the code cannot produce"
        )

    def test_anchored_refusal_is_not_reachable_from_a_subprocess(self):
        """The whole point: no `sys.exit(<a subprocess returncode>)` in cmd_run,
        because that hands a downstream tool the ability to forge exit 2."""
        src = (SRC / "cmd_run.py").read_text(encoding="utf-8")
        assert "sys.exit(result.returncode)" not in src, (
            "cmd_run returns a subprocess's exit code, so pxx can forge "
            "EXIT_ANCHORED_REFUSED"
        )


class TestNoLabAddressesAnywhere:
    """`VISION.md` red line: no real lab addresses in tracked files. It was
    enforced for `TUTORIAL.md` alone, so writing one into `checkpoint.md` sailed
    through 327 green tests and was pushed — while documenting the fix for the
    identical problem in a sibling repo. The narrow guard was the bug.

    RFC1918 and RFC6598 are rejected everywhere in the tree. Loopback and the
    RFC-5737 documentation ranges are fine: those are what examples should use.
    """

    _PRIVATE = re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])"
        r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}(?:\.\d{1,3})?\b"
    )

    @staticmethod
    def _tracked_text_files():
        import subprocess

        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=30
        )
        for rel in out.stdout.splitlines():
            if rel.endswith((".md", ".py", ".sh", ".yml", ".yaml", ".toml", ".json")):
                path = ROOT / rel
                if path.is_file():
                    yield rel, path.read_text(encoding="utf-8", errors="replace")

    def test_no_private_range_address_in_any_tracked_file(self):
        offenders = [
            f"{rel}:{text[:m.start()].count(chr(10)) + 1}: {m.group(0)}"
            for rel, text in self._tracked_text_files()
            for m in self._PRIVATE.finditer(text)
        ]
        assert not offenders, (
            "real private-range addresses in tracked files (VISION.md red line); "
            "use RFC-5737 documentation ranges instead:\n  " + "\n  ".join(offenders)
        )

    def test_the_guard_actually_matches_something(self):
        """A red-line guard whose pattern never fires is decoration.

        The probes are assembled from parts on purpose: written literally they
        would be found by the scan above, in this very file.
        """
        blocked = ["10." + "0.1.125", "192." + "168.1.1", "172." + "16.0.9"]
        allowed = ["127." + "0.0.1", "192." + "0.2.125", "203." + "0.113.10"]
        for probe in blocked:
            assert self._PRIVATE.search(probe), f"guard missed {probe}"
        for probe in allowed:
            assert not self._PRIVATE.search(probe), f"guard wrongly flagged {probe}"
