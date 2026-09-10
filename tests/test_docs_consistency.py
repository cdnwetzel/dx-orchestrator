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

    def test_no_duplicate_changelog_version_headings(self):
        """Two driver boxes push to one repo, so the same version number can be
        cut twice — 0.9.0/0.9.1 collided exactly this way (9074854 and e6bf51e
        both landed as 0.9.1). A duplicate `## [X.Y.Z]` heading means one
        release's notes silently overwrote another's in the reader's eyes. The
        existing link-definition check only catches a *missing* link, never a
        repeated heading, so this is the guard that turns the collision from
        'a human noticed' into a build failure. Before cutting a version, also
        run `git fetch` + `git ls-remote --heads origin` to see the other box's
        in-flight work."""
        headings = re.findall(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", CHANGELOG, re.MULTILINE)
        seen: set[str] = set()
        dupes = sorted({v for v in headings if v in seen or seen.add(v)})
        assert not dupes, f"CHANGELOG.md has duplicate version heading(s): {dupes}"


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

    def test_every_clone_dependent_test_carries_its_own_skip(self):
        """A file-level check cannot see *which* function a decorator landed on.

        0.10.0 inserted a helper between `@pytest.mark.skipif` and the
        merge-transcript test, so the decorator guarded the helper and CI ran
        the test with no clone present. Both development boxes had the clone,
        so it could not show locally. Check the marker on each function that
        actually reaches for a clone.
        """
        import inspect
        import sys

        module = sys.modules[__name__]
        reaches = re.compile(r"REF_LEDGER|REAL_CARDS|self\._merge\((?![^)]*ledger=)")
        offenders = []
        for cls in (obj for obj in vars(module).values() if inspect.isclass(obj)):
            for name, fn in vars(cls).items():
                if not name.startswith("test_"):
                    continue
                if name == "test_every_clone_dependent_test_carries_its_own_skip":
                    continue  # this test's own source names the constants it hunts for
                if reaches.search(inspect.getsource(fn)) and not any(
                    m.name == "skipif" for m in getattr(fn, "pytestmark", [])
                ):
                    offenders.append(f"{cls.__name__}.{name}")
        assert not offenders, (
            "tests that need a sibling clone but carry no skipif of their own: "
            f"{offenders}"
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


def _subprocess_path() -> str:
    """System dirs only — plus wherever gpg lives, since `dx merge` shells out to
    it and Homebrew puts it in /opt/homebrew/bin, outside /usr/bin:/bin."""
    import shutil

    dirs = ["/usr/bin", "/bin"]
    gpg = shutil.which("gpg")
    if gpg and str(Path(gpg).parent) not in dirs:
        dirs.append(str(Path(gpg).parent))
    return ":".join(dirs)


_SUBPROCESS_PATH = _subprocess_path()


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
                "PATH": _SUBPROCESS_PATH,
                "HOME": str(Path.home()),
                "PYTHONPATH": str(ROOT / "src"),
            },
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip("\n") in TUTORIAL, (
            "the `dx roles list --fit High` transcript in TUTORIAL.md no longer "
            "matches what that command prints:\n" + result.stdout
        )


REF_LEDGER = Path("~/ai/devswarm-ledger-reference").expanduser()


class TestMergeTranscriptFidelity:
    """§7 is the tutorial's most load-bearing section and nothing checked it.

    When 0.7.0 repointed DEFAULT_LEDGER_REPO, §7 still told readers to run
    `dx merge T-0007` — a task that exists only in a private operational ledger.
    Every new reader got "queue file not found" from the section demonstrating
    the gate the whole project is about, and 337 tests stayed green.
    """

    @staticmethod
    def _merge(task_id, *extra, ledger=None):
        """Runs against a *disposable copy* of the reference ledger.

        `dx merge` was read-only when these tests were written. It writes now —
        and with DX_LEDGER_REPO unset it resolves to the operator's own clone at
        ~/ai/devswarm-ledger-reference. This test appended SIGNED, MERGED and
        UNLOCK commits to it on every suite run before that was noticed. Any
        test invoking a writing command needs its own target.
        """
        import shutil
        import subprocess
        import tempfile

        env = {
            "PATH": _SUBPROCESS_PATH,
            "HOME": str(Path.home()),
            "PYTHONPATH": str(ROOT / "src"),
        }
        with tempfile.TemporaryDirectory(prefix="dx-ledger-copy-") as tmp:
            copy = Path(tmp) / "ledger"
            shutil.copytree(ledger or REF_LEDGER, copy)
            env["DX_LEDGER_REPO"] = str(copy)
            return subprocess.run(
                [sys.executable, "-m", "dx.cli", "merge", task_id, *extra],
                capture_output=True, text=True, env=env,
            )

    def test_the_pxx_failure_line_matches_what_cmd_run_prints(self):
        """§6 quoted "pxx task failed." for two releases after 0.7.1 changed it
        to carry pxx's own code — the change that stopped a failing task looking
        like a governance refusal. A tutorial quoting the old line teaches the
        old contract."""
        src = (SRC / "cmd_run.py").read_text(encoding="utf-8")
        assert "pxx task failed (pxx exit {result.returncode})" in src, (
            "cmd_run's failure message changed; update this guard and §6"
        )
        assert "❌ pxx task failed (pxx exit 2)." in TUTORIAL, (
            "TUTORIAL.md §6 quotes a pxx failure line that cmd_run no longer prints"
        )

    def test_the_exit_codes_shown_are_the_ones_dx_defines(self):
        """§6 claimed exit 2 for a failing task, which is now the code reserved
        for an Anchored refusal — the exact confusion 0.7.1 removed."""
        from dx import cmd_run

        assert f"Exit code `{cmd_run.EXIT_TASK_FAILED}`" in TUTORIAL, (
            "TUTORIAL.md §6 no longer states the task-failure exit code dx uses"
        )

    def test_the_task_id_shown_exists_in_the_default_ledger(self):
        """Cheap, hermetic, and enough on its own to have caught the break: the
        task the tutorial tells you to merge must live in the ledger dx ships
        with, not in someone's private one."""
        from dx.config_loader import DEFAULT_LEDGER_REPO

        expected = {
            q.stem
            for q in sorted((DEFAULT_LEDGER_REPO / "queue").glob("*.json"))
            if q.name != "MERGE_LOCK.json"
        } or {"T-0001"}   # ledger not cloned: fall back to the shipped task id

        shown = re.findall(r"^dx merge (\S+)", TUTORIAL, re.MULTILINE)
        assert shown, "TUTORIAL.md no longer shows a `dx merge` command"
        for task_id in set(shown):
            assert task_id in expected, (
                f"TUTORIAL.md tells the reader to run `dx merge {task_id}`, but "
                f"the ledger dx defaults to holds {sorted(expected)}. A reader "
                f"following the tutorial gets 'queue file not found'."
            )

    @staticmethod
    def _blur(text):
        """Ledger rows carry a timestamp, so every append yields a different
        head. Pin the wording, not the digest."""
        return re.sub(r"\b[0-9a-f]{8,64}\b", "<hash>", text)

    @pytest.mark.skipif(
        not REF_LEDGER.is_dir(),
        reason="devswarm-ledger-reference not cloned; CI clones it, so this runs there",
    )
    def test_the_green_merge_transcript_is_reproducible(self):
        result = self._merge("T-0001")
        assert result.returncode == 0, result.stderr
        blurred = self._blur(TUTORIAL)
        for raw in result.stdout.strip().splitlines():
            line = self._blur(raw)
            assert line.strip() in blurred, (
                "the `dx merge T-0001` transcript in TUTORIAL.md no longer matches "
                f"what that command prints. Missing:\n  {line}"
            )

    @pytest.mark.skipif(
        not REF_LEDGER.is_dir(),
        reason="devswarm-ledger-reference not cloned; CI clones it, so this runs there",
    )
    @pytest.mark.skipif(
        not REF_LEDGER.is_dir(),
        reason="devswarm-ledger-reference not cloned; CI clones it, so this runs there",
    )
    def test_a_green_merge_binds_its_bundle_into_the_ledger(self, tmp_path):
        """A1: the append-only chain must commit to the evidence, not just the
        event. A green merge appends an EVIDENCE row whose value is the bundle's
        digest, and the chain still verifies."""
        import json
        import shutil
        import subprocess

        copy = tmp_path / "ledger"
        shutil.copytree(REF_LEDGER, copy)
        ev = tmp_path / "ev"
        env = {
            "PATH": _SUBPROCESS_PATH,
            "HOME": str(Path.home()),
            "PYTHONPATH": str(ROOT / "src"),
            "DX_LEDGER_REPO": str(copy),
        }
        r = subprocess.run(
            [sys.executable, "-m", "dx.cli", "merge", "T-0001", "--evidence-dir", str(ev)],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        rows = [json.loads(x) for x in (copy / "ledger.jsonl").read_text().splitlines()]
        last = rows[-1]
        assert last["action"] == "EVIDENCE"
        assert last["evidence"].startswith("dx.merge_gate.v1 sha256:")
        # the recorded digest is the actual bundle's digest
        from dx.evidence import bundle_digest
        bundle = sorted((ev / "T-0001").glob("*"))[-1]
        assert bundle_digest(bundle) in last["evidence"]
        assert "binds the merge-gate bundle" in r.stderr

    @pytest.mark.skipif(
        not REF_LEDGER.is_dir(),
        reason="devswarm-ledger-reference not cloned; CI clones it, so this runs there",
    )
    def test_the_force_banner_transcript_is_reproducible(self):
        result = self._merge("T-0001", "--force")
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        blurred = self._blur(TUTORIAL)
        for raw in combined.strip().splitlines():
            line = self._blur(raw)
            assert line.strip() in blurred, (
                "the `dx merge --force` transcript in TUTORIAL.md drifted. "
                f"Missing:\n  {line}"
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


class TestUsageAndSpecTemplate:
    """USAGE.md and the spec template are the tool's front door for a coding
    task. They only work if the template keeps the fields the workflow depends
    on and USAGE keeps pointing at the real command."""

    USAGE = ROOT / "USAGE.md"
    SPEC = ROOT / "templates" / "spec.md"
    EXAMPLE = ROOT / "templates" / "spec.example.md"

    def test_the_files_exist(self):
        assert self.USAGE.is_file() and self.SPEC.is_file() and self.EXAMPLE.is_file()

    def test_usage_shows_the_real_run_command(self):
        text = self.USAGE.read_text(encoding="utf-8")
        assert "dx run --required_role" in text
        assert "templates/spec.md" in text  # points at the template

    def test_readme_links_usage(self):
        assert "USAGE.md" in README

    @pytest.mark.skipif(
        not REAL_CARDS.is_dir(),
        reason="sdlc-agent-roles not cloned; CI clones it, so this runs there",
    )
    def test_the_readme_role_card_count_matches_the_deck(self):
        """The README cites a card count as a governance claim. The deck lives in
        a sibling repo, so nothing checked dx's number against it — and it drifted
        (38 stated while the deck shipped 40 after the staging pair). Now a build
        failure, not a human noticing."""
        deck = len(list(REAL_CARDS.glob("*.md")))
        claimed = {int(n) for n in re.findall(r"(\d+)\s+(?:governance )?role cards", README)}
        assert claimed == {deck}, (
            f"README states role-card count(s) {sorted(claimed)}, but the deck has {deck}"
        )

    def test_the_template_keeps_the_fields_the_workflow_needs(self):
        """These are the inputs dx wraps and the role cards expect. Dropping one
        is how results stop being consistent."""
        text = self.SPEC.read_text(encoding="utf-8")
        for field in (
            "**Role:**",
            "**Scope:**",
            "## 1. Objective",
            "## 2. Non-goals",
            "## 3. Allowed and prohibited paths",
            "## 5. Acceptance criteria",
            "## 6. Reviewer",
        ):
            assert field in text, f"spec template lost a required field: {field}"

    def test_the_example_is_a_filled_version_of_the_template(self):
        """The example must carry the same section skeleton as the template, so
        it stays a working model rather than drifting into a different shape."""
        example = self.EXAMPLE.read_text(encoding="utf-8")
        for heading in (
            "## 1. Objective",
            "## 2. Non-goals",
            "## 3. Allowed and prohibited paths",
            "## 5. Acceptance criteria",
            "## 6. Reviewer",
        ):
            assert heading in example, f"example spec is missing {heading}"
        assert "dx run --required_role" in example  # shows how to run itself
