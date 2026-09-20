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

# A role-card count is a governance claim wherever it appears, in whatever
# phrasing. These patterns catch the forms the docs actually use so a count can
# be policed in ANY tracked file, not just the README (dx #6).
_CARD_COUNT_PATTERNS = (
    # One general form, not a list of the exact sentences we happened to write:
    # "40 role cards", "40 governance role cards", "38 cards", "40-card deck".
    #
    # Horizontal whitespace only — `\s` matches newlines, so a number ending one
    # line and "cards" opening the next would be read as a claim that neither
    # sentence makes. The accepted cost is that a count wrapped mid-phrase across
    # a line break is not seen; a false positive here fails the build loudly,
    # while this kind of false negative is the silence the guard is built to
    # avoid, so the limitation is asserted below rather than left to be found.
    r"\b(\d+)[ \t-]*(?:governance[ \t]+)?(?:role[ \t]+)?cards?\b",
    r"role cards?[ \t]*\(one of (\d+)",         # "role card (one of 40"
    r"(\d+)[ \t]+files at\b[^\n]*roles",       # doctor sample: "40 files at .../roles"
)

#: A count may disagree with today's deck when it is deliberately a record of the
#: past — the archived predecessor deck, or a captured session transcript — and
#: says so on the spot. Declared, never silent: the same posture the manifest
#: generator takes with `governed: false` + a required reason. A bare stale count
#: is still a failure; an exemption must be visible in the diff and give a reason.
_HISTORICAL_MARK = re.compile(
    r"<!--\s*deck-count:\s*historical\s*[-\u2014:]\s*\S[^>]*-->", re.IGNORECASE
)


#: A fence opens with three or more backticks or tildes and closes only on the
#: SAME character, at least as long (CommonMark). Toggling on any fence lets a
#: literal ``` line inside a ~~~ block close it and re-expose what follows.
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
#: Inline code spans match on their complete backtick run, so ``x`` is masked as
#: one span rather than leaving `x` exposed between two single-backtick matches.
_INLINE_CODE = re.compile(r"(`+).*?\1")


def _active_marker_lines(text: str) -> set[int]:
    """Line indices carrying a *live* historical marker.

    A marker written as an example — inside a code span or a fenced block — must
    not exempt anything. `CHANGELOG.md` documents this very marker as inline
    code, so without this the documentation of an escape hatch would become an
    escape hatch, silently, for whatever count happened to sit beside it.

    Both delimiter forms are parsed properly rather than approximately, because
    an approximate parse here fails in the permissive direction: every miss is a
    count that stops being checked, and it stops quietly.
    """
    lines = text.splitlines()
    active: set[int] = set()
    fence: str | None = None
    for i, line in enumerate(lines):
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
                continue
            if marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        if _HISTORICAL_MARK.search(_INLINE_CODE.sub("", line)):
            active.add(i)
    return active


def _role_card_counts(text: str, *, skip_marked: bool = False) -> set[int]:
    """Card counts stated in ``text``.

    With ``skip_marked``, a count on a line carrying (or directly under) a
    ``<!-- deck-count: historical - why -->`` marker is not returned: it is a
    deliberate record of the past rather than a claim about today's deck.
    """
    marked = _active_marker_lines(text) if skip_marked else set()
    counts: set[int] = set()
    for pat in _CARD_COUNT_PATTERNS:
        for m in re.finditer(pat, text):
            index = text[: m.start()].count("\n")
            if skip_marked and (index in marked or index - 1 in marked):
                continue
            counts.add(int(next(g for g in m.groups() if g)))
    return counts


#: Binary sniffing beats an extension list. A NUL byte in the first block is the
#: same heuristic git itself uses to call a file binary.
_BINARY_SNIFF_BYTES = 8192


def _tracked_text_files(
    suffixes: tuple[str, ...] | None = None,
) -> list[tuple[str, str]]:
    """Every tracked text file, as (relpath, text) — or only those with the given
    suffixes when the caller genuinely means a subset.

    Shared by the red-line address guard and the role-card count guard: both
    police a claim that can appear in any file, so neither may carry its own
    hand-maintained file list.

    The default is *every* tracked file that is not binary, and that matters.
    This helper used to filter on a tuple of extensions, which quietly excluded
    `LICENSE`, `.gitignore`, the `.asc` fixtures and — worst — the committed live
    ledger at `docs/artifacts/*.jsonl`. A lab address in any of them sailed past
    the red line. An extension allowlist IS the hand-maintained list this module
    keeps re-learning not to write; the file type is sniffed instead, so a new
    kind of tracked file is covered the day it lands rather than the day someone
    remembers to add it.

    The count guard still passes `(".md",)` on purpose — prose stating a card
    count is a claim about the real deck, while the same digits in code or a
    fixture describe themselves.

    Discovery failing is a *test* failure, never an empty result. If `git
    ls-files` exits nonzero — an exported tree, a damaged checkout — an unchecked
    run hands back empty stdout, and both repo-wide guards then sweep zero files
    and report green. That is the same silent pass this guard exists to remove,
    one layer further down, so the subprocess is checked and an empty sweep is
    refused outright.

    `-z` for the same reason. Plain `git ls-files` *quotes* any path with a
    special or non-ASCII character (`"\303\251.md"`), and a quoted name no
    longer ends in `.md`, so the file drops out of the sweep — silently, and in
    exactly the direction that matters: the file nobody can read easily is the
    one that would carry a leaked address. NUL-delimited output is never quoted.
    """
    import os
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    found: list[tuple[str, str]] = []
    for rel in out.stdout.split("\0"):
        if not rel or (suffixes is not None and not rel.endswith(suffixes)):
            continue
        path = ROOT / rel
        if path.is_symlink():
            # What git tracks for a symlink is the TARGET STRING, so that string
            # is the content to police. Following the link instead would read a
            # file outside the repository — possibly clean, while the tracked
            # bytes carry the address — and a dangling link would vanish from the
            # sweep entirely. Neither is the guard doing its job.
            found.append((rel, os.readlink(path)))
            continue
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw[:_BINARY_SNIFF_BYTES]:
            continue  # binary fixture; nothing to read a claim out of
        found.append((rel, raw.decode("utf-8", errors="replace")))
    assert found, (
        f"no tracked files matching {suffixes} were found — a guard that scans "
        "nothing passes for the wrong reason"
    )
    return found


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
            # The subprocess gets a custom env, so it does NOT inherit the conftest
            # DX_EVIDENCE_DIR redirect and HOME is the real home — without this a
            # green `dx merge T-0001` writes a real bundle into ~/.local/state/dx/
            # evidence every suite run. Confine it to the throwaway dir.
            env["DX_EVIDENCE_DIR"] = str(Path(tmp) / "evidence")
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

    def test_no_private_range_address_in_any_tracked_file(self):
        offenders = [
            f"{rel}:{text[:m.start()].count(chr(10)) + 1}: {m.group(0)}"
            for rel, text in _tracked_text_files()
            for m in self._PRIVATE.finditer(text)
        ]
        assert not offenders, (
            "real private-range addresses in tracked files (VISION.md red line); "
            "use RFC-5737 documentation ranges instead:\n  " + "\n  ".join(offenders)
        )

    def test_discovery_is_nul_delimited_so_odd_names_cannot_hide(self, monkeypatch):
        """`git ls-files` quotes a path containing a special or non-ASCII byte,
        and a quoted name no longer ends in `.md`, so it silently leaves the
        sweep. That is backwards: the awkward filename is the likelier place for
        a leaked address, not the less likely. `-z` never quotes."""
        import subprocess

        seen: dict = {}
        real = subprocess.run

        def _spy(args, **kwargs):
            seen["args"] = args
            return real(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        files = _tracked_text_files((".md",))
        assert "-z" in seen["args"], "discovery must use NUL-delimited output"
        assert any(rel == "README.md" for rel, _ in files)

    def test_paths_are_split_on_nul_not_newlines(self, monkeypatch):
        """The other half of `-z`: splitting NUL output on newlines would glue
        every path into one unmatchable string."""
        import subprocess

        class _Out:
            stdout = "README.md\0VISION.md\0"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Out())
        assert {rel for rel, _ in _tracked_text_files((".md",))} == {"README.md", "VISION.md"}

    def test_a_failed_file_discovery_fails_the_guard(self, monkeypatch):
        """The silent pass one layer down: if `git ls-files` errors, an unchecked
        run hands back empty stdout and every repo-wide guard sweeps nothing and
        reports green. Discovery failure must be loud."""
        import subprocess

        def _broken(*args, **kwargs):
            raise subprocess.CalledProcessError(128, "git ls-files")

        monkeypatch.setattr(subprocess, "run", _broken)
        with pytest.raises(subprocess.CalledProcessError):
            _tracked_text_files()

    def test_an_empty_sweep_is_refused(self, monkeypatch):
        """The other half: a clean exit matching nothing is equally a guard that
        scanned zero files, so it is refused rather than reported green."""
        import subprocess

        class _Empty:
            stdout = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Empty())
        with pytest.raises(AssertionError, match="scans nothing"):
            _tracked_text_files()

    def test_it_sees_tracked_files_an_extension_list_would_miss(self):
        """The guard filtered on a tuple of extensions, so `LICENSE`,
        `.gitignore`, the `.asc` fixtures and the committed live ledger
        (`docs/artifacts/*.jsonl`) were never scanned. The ledger is the one that
        stings: it is real captured evidence, exactly where a stray address would
        end up, and it was invisible to the red line."""
        scanned = {rel for rel, _ in _tracked_text_files()}
        for rel in ("LICENSE", ".gitignore"):
            assert rel in scanned, f"{rel} is tracked text and must be scanned"
        assert any(rel.endswith(".jsonl") for rel in scanned), (
            "the committed ledger artifact must be scanned for addresses"
        )

    def test_type_is_sniffed_from_content_not_from_the_name(self):
        """`tests/fixtures/gpg/payload.bin` is ASCII despite the extension, and it
        IS scanned. That is the argument for the change in one file: the name said
        binary, the bytes said text, and a name-based rule would have skipped a
        readable tracked file on the strength of three characters."""
        scanned = {rel for rel, _ in _tracked_text_files()}
        assert "tests/fixtures/gpg/payload.bin" in scanned

    def test_a_symlink_is_scanned_by_its_target_string_not_followed(
        self, tmp_path, monkeypatch
    ):
        """git tracks a symlink's target string, so that is the tracked content.
        Following the link reads a file outside the repo — which can be perfectly
        clean while the tracked bytes carry the address — and a dangling link
        disappears from the sweep altogether. No tracked symlink exists here
        today; this keeps the gap shut before one does."""
        import subprocess

        (tmp_path / "clean.txt").write_text("nothing to see\n")
        (tmp_path / "ok").symlink_to("clean.txt")
        (tmp_path / "leak").symlink_to("../fleet/10." + "0.1.125/config")
        (tmp_path / "dangling").symlink_to("nowhere-at-all")
        monkeypatch.setattr("test_docs_consistency.ROOT", tmp_path, raising=False)

        class _Out:
            stdout = "ok\0leak\0dangling\0"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Out())
        scanned = dict(_tracked_text_files())
        assert set(scanned) == {"ok", "leak", "dangling"}, (
            "a dangling symlink must not drop out of the sweep"
        )
        assert scanned["ok"] == "clean.txt", "the link target string, not the file"
        offenders = [rel for rel, text in scanned.items() if self._PRIVATE.search(text)]
        assert offenders == ["leak"]

    def test_a_file_with_nul_bytes_is_skipped(self, tmp_path, monkeypatch):
        """The other direction: scanning everything only works ifbinary content
        is skipped, or the guard trades a silent gap for decode noise. No tracked
        file is binary today, so the branch is exercised synthetically rather than
        left unproven."""
        import subprocess

        (tmp_path / "blob").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
        (tmp_path / "plain.md").write_text("readable\n")
        monkeypatch.setattr("test_docs_consistency.ROOT", tmp_path, raising=False)

        class _Out:
            stdout = "blob\0plain.md\0"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Out())
        assert {rel for rel, _ in _tracked_text_files()} == {"plain.md"}

    def test_an_address_in_an_extensionless_file_is_caught(self, tmp_path, monkeypatch):
        """The failure the extension list allowed, proven rather than argued: a
        private-range address in a tracked file with no recognised suffix."""
        import subprocess

        (tmp_path / "LICENSE").write_text("Contact 10." + "0.1.125 for terms\n")
        monkeypatch.setattr(
            "test_docs_consistency.ROOT", tmp_path, raising=False
        )

        class _Out:
            stdout = "LICENSE\0"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Out())
        offenders = [
            rel
            for rel, text in _tracked_text_files()
            if self._PRIVATE.search(text)
        ]
        assert offenders == ["LICENSE"]

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
    def test_role_card_counts_match_the_deck_in_every_doc(self):
        """A card count is a governance claim wherever it appears, so the guard
        reads every tracked doc — it does not carry a list of which ones.

        Twice now the narrow version has been the bug. First the count was policed
        in README.md alone and drifted in TUTORIAL.md (dx #6, the 0.7.2 lesson).
        Then the fix hardcoded *two* files and told the next author to "extend the
        tuple" — so RELEASE_READINESS.md said 38 while the deck held 40, and
        VISION.md and a second RELEASE_READINESS line stated 40 by luck rather
        than by enforcement. A guard that needs manual extension is a guard that
        is one forgotten edit from silence. Every tracked `*.md` is checked, in
        every phrasing the docs use.

        Markdown only, on purpose: prose stating a count is making a claim about
        the real deck, whereas a count in test code is describing its own fixture
        (`test_corrupt_role_cards.py` legitimately builds a deck of 1).
        """
        deck = len(list(REAL_CARDS.glob("*.md")))
        offenders = [
            f"{rel}: states {sorted(counts)}"
            for rel, text in _tracked_text_files((".md",))
            for counts in [_role_card_counts(text, skip_marked=True)]
            if not counts <= {deck}
        ]
        assert not offenders, (
            f"role-card counts in tracked docs disagree with the deck ({deck} cards):\n  "
            + "\n  ".join(offenders)
        )
        # and the README must still state the count in a recognized form, so a
        # phrasing change cannot make the guard silently find nothing.
        assert deck in _role_card_counts(README), (
            f"README no longer states the deck count {deck} in a form the guard recognizes"
        )

    def test_every_count_pattern_still_matches_something(self):
        """A pattern that never fires is decoration — the same standard the
        red-line address guard holds itself to. These probes are the phrasings
        the docs actually use, including the two that escaped the earlier guard
        ('38 cards' with no 'role', and the hyphenated '38-card deck')."""
        probes = {
            "40 role cards": 40,
            "40 governance role cards": 40,
            "the same 38 cards at the identical path": 38,
            "the real 38-card deck is absent": 38,
            "every role card (one of 40, shipped in": 40,
            "Role cards parse cleanly (40 files at /x/roles)": 40,
        }
        for text, expected in probes.items():
            assert expected in _role_card_counts(text), f"guard no longer reads {text!r}"

    def test_a_count_claim_never_spans_a_line_break(self):
        """`\\s` would have matched newlines, so a table cell ending in a number
        and a following line opening with "cards" read as a count neither line
        states. The other half of the trade-off is recorded too: a phrase wrapped
        across a break is genuinely not seen."""
        assert _role_card_counts("| ... | 38 |\ncards are listed above") == set()
        assert _role_card_counts("...had 38\n\ncards") == set()
        assert _role_card_counts("38 cards") == {38}          # same line, matched
        assert _role_card_counts("38 role\ncards") == set()   # wrapped: the known gap

    def test_a_marked_historical_count_is_exempt_and_a_bare_one_is_not(self):
        """The exemption must work, and must not be a blanket one: the marker
        covers its own line and the line under it, nothing further."""
        marker = "<!-- deck-count: historical - the archived deck -->"
        assert _role_card_counts(f"{marker}\n38 role cards", skip_marked=True) == set()
        assert _role_card_counts(f"38 role cards {marker}", skip_marked=True) == set()
        # one line too far, and a wholly unmarked line, both still count
        assert 38 in _role_card_counts(f"{marker}\n\n38 role cards", skip_marked=True)
        assert 38 in _role_card_counts("38 role cards", skip_marked=True)

    def test_a_marker_shown_as_an_example_does_not_exempt(self):
        """CHANGELOG.md documents this marker as inline code. Without excluding
        code spans and fences, the documentation of an escape hatch silently
        BECOMES one for whatever count happens to sit beside it — the exemption
        equivalent of a guard that cannot fire."""
        shown = "A count may be exempted by `<!-- deck-count: historical - why -->`"
        assert 38 in _role_card_counts(f"{shown}\n38 role cards", skip_marked=True)
        fenced = "```\n<!-- deck-count: historical - why -->\n38 role cards\n```"
        assert 38 in _role_card_counts(fenced, skip_marked=True)
        # the real thing, unquoted, still exempts
        real = "<!-- deck-count: historical - the archived deck -->"
        assert _role_card_counts(f"{real}\n38 role cards", skip_marked=True) == set()

    def test_code_delimiters_are_parsed_not_approximated(self):
        """Both misses found in review, pinned. A double-backtick span left the
        marker exposed between two single-backtick matches; and toggling fence
        state on any delimiter let a literal ``` line close a ~~~ block, so a
        marker after it read as live. An approximate Markdown parse fails in the
        permissive direction here — every miss is a count that stops being
        checked, quietly."""
        marker = "<!-- deck-count: historical - why -->"
        double = f"Shown as ``{marker}``\n38 role cards"
        assert 38 in _role_card_counts(double, skip_marked=True), "double-backtick span"
        crossed = f"~~~\n```\n{marker}\n38 role cards\n~~~"
        assert 38 in _role_card_counts(crossed, skip_marked=True), "mismatched fence"
        # a fence really does still suppress, and a real marker really does exempt
        fenced = f"```\n{marker}\n38 role cards\n```"
        assert 38 in _role_card_counts(fenced, skip_marked=True)
        assert _role_card_counts(f"{marker}\n38 role cards", skip_marked=True) == set()

    def test_an_exemption_without_a_reason_does_not_exempt(self):
        """`governed: false` requires a reason; so does this. A bare opt-out is
        how an exemption stops being a declaration and becomes a silence."""
        for bare in ("<!-- deck-count: historical -->", "<!-- deck-count: historical - -->"):
            assert 38 in _role_card_counts(f"{bare}\n38 role cards", skip_marked=True), (
                f"{bare!r} exempted a count without saying why"
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


class TestReviewBotConfiguration:
    """`.coderabbit.yaml` carries the review rules written from this repo's own
    defect history (ported from PR #27's Greptile config after that trial
    ended). Two properties would fail silently if lost, so they are pinned."""

    CONFIG = ROOT / ".coderabbit.yaml"

    def _config(self):
        import yaml

        assert self.CONFIG.is_file(), ".coderabbit.yaml is missing; the review rules are gone"
        try:
            return yaml.safe_load(self.CONFIG.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # pragma: no cover - the message is the point
            raise AssertionError(f".coderabbit.yaml is not valid YAML: {exc}") from None

    def test_it_re_reviews_when_a_fix_is_pushed(self):
        """A review pinned to a commit the branch has moved past reads as
        coverage while the fix nobody looked at rides along underneath it."""
        auto = self._config()["reviews"]["auto_review"]
        assert auto.get("enabled") is True
        assert auto.get("auto_incremental_review") is True, (
            "auto_incremental_review must stay on, or a post-review fix goes unreviewed"
        )

    def test_it_stays_advisory(self):
        """A review bot informs a merge; CI and a human decide it."""
        assert self._config()["reviews"].get("request_changes_workflow") is False

    def test_every_rule_from_the_repos_history_is_present(self):
        """The rules exist because each encodes a defect that shipped here. If
        one is edited out, the config still parses and nothing else notices."""
        # The block scalar is hard-wrapped; a marker may span a line break.
        text = " ".join(self._config()["reviews"]["path_instructions"][0]["instructions"].split())
        for marker in (
            "A GUARD MUST NOT CARRY A HAND-MAINTAINED LIST",
            "EVERY GUARD NEEDS A NEGATIVE CONTROL",
            "DECLARED, NEVER SILENT",
            "NO REAL FLEET ADDRESSES IN TRACKED FILES",
            "THE LEDGER AND THE EVIDENCE STORE ARE APPEND-ONLY",
            "DERIVED BY THE VERIFIER, NEVER ASSERTED BY THE SIGNER",
            "THE AGENT NEVER GRADUATES A SEAT",
            "EVIDENCE BEATS ASSERTION IN PROSE TOO",
            "LOCAL MODELS NEVER GATE",
            "SEVERITY DISCIPLINE",
        ):
            assert marker in text, f"review rule missing: {marker}"
        assert len(text) <= 20_000, "CodeRabbit caps path instructions at 20,000 characters"

    def test_the_guideline_documents_it_cites_exist(self):
        """A code_guidelines pattern naming a file that is not there is a rule
        that silently reads nothing."""
        for pattern in self._config()["knowledge_base"]["code_guidelines"]["filePatterns"]:
            assert list(ROOT.glob(pattern)), f"code_guidelines names a missing file: {pattern}"
