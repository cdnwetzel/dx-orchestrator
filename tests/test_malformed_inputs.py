"""Operator-input failures must stop the line with a message, not a traceback.

The hardware manifest is hand-edited by design — the tutorial tells you to edit
it — so a wrong shape is an ordinary mistake, not an exotic case. The ledger can
be corrupted by a bad merge. In both situations dx has exactly one job: say what
is wrong and refuse to continue.

Before these fixes, `dx run` on a mangled manifest printed a Python traceback,
and `dx merge` on a corrupt ledger printed *two green checkmarks* and then
crashed with a JSONDecodeError — the worst possible shape for a gate failure.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from dx.cli import main
from dx.config_loader import ConfigError, get_gui_config, get_route_for_role, validate_manifest
from dx.ledger_utils import (
    GPG_TIMEOUT_S,
    LedgerError,
    get_ledger_head,
    get_task_author_human,
    get_task_queue,
)

MALFORMED_MANIFESTS = [
    ("roles as a list", "roles:\n  - backend-engineer\n", "roles"),
    ("roles as a scalar", "roles: nope\n", "roles"),
    ("role entry as a scalar", "roles:\n  widget-engineer: http://h:1\n", "roles.widget-engineer"),
    ("default entry as a scalar", "roles:\n  default: http://h:1\n", "roles.default"),
    ("top level a list", "- a\n- b\n", "the manifest"),
    ("top level a scalar", "just a string\n", "the manifest"),
]


@pytest.fixture
def manifest(monkeypatch, tmp_path):
    def _write(content: str):
        path = tmp_path / "manifest.yml"
        path.write_text(content, encoding="utf-8")
        monkeypatch.setenv("DX_CONFIG", str(path))
        return path

    return _write


class TestManifestShape:
    @pytest.mark.parametrize(
        "label,content,offender",
        MALFORMED_MANIFESTS,
        ids=[m[0] for m in MALFORMED_MANIFESTS],
    )
    def test_wrong_shape_raises_config_error(self, manifest, label, content, offender):
        manifest(content)
        with pytest.raises(ConfigError) as exc:
            get_route_for_role("widget-engineer")
        assert offender in str(exc.value)

    def test_the_error_names_the_file(self, manifest):
        path = manifest("roles:\n  - a\n")
        with pytest.raises(ConfigError) as exc:
            get_route_for_role("any")
        assert str(path) in str(exc.value)

    def test_the_error_suggests_the_actual_cause(self, manifest):
        """A leading '-' is how this mistake is nearly always made."""
        manifest("roles:\n  - a\n")
        with pytest.raises(ConfigError) as exc:
            get_route_for_role("any")
        assert "indentation" in str(exc.value)

    def test_invalid_yaml_raises_config_error_not_yaml_error(self, manifest):
        manifest("roles: [unclosed\n")
        with pytest.raises(ConfigError) as exc:
            get_route_for_role("any")
        assert "invalid YAML" in str(exc.value)

    def test_wrong_shaped_gui_section_raises(self, manifest):
        manifest("gui_verification:\n  - a\n")
        with pytest.raises(ConfigError):
            get_gui_config()

    def test_empty_manifest_is_valid(self, manifest):
        """An empty file means 'no overrides', not 'malformed'."""
        manifest("")
        validate_manifest()
        assert get_route_for_role("any").endpoint == "http://localhost:11434"

    def test_validate_manifest_checks_every_role_not_just_the_one_in_use(self, manifest):
        """Regression: doctor validated only the default route, so a malformed
        entry for another role passed the self-test and failed on first use."""
        manifest(
            'roles:\n'
            '  default:\n    endpoint: "http://ok.invalid:1"\n'
            '  widget-engineer: "oops-a-string"\n'
        )
        get_route_for_role("default")  # the in-use path is fine
        with pytest.raises(ConfigError) as exc:
            validate_manifest()
        assert "roles.widget-engineer" in str(exc.value)


class TestLedgerCorruption:
    @pytest.fixture
    def ledger(self, tmp_path):
        def _write(content: str):
            repo = tmp_path / "ledger"
            repo.mkdir(exist_ok=True)
            (repo / "ledger.jsonl").write_text(content, encoding="utf-8")
            return repo

        return _write

    def test_corrupt_row_raises_ledger_error_not_jsondecodeerror(self, ledger):
        repo = ledger('{"task_id": "T-1", "author_human": "A"}\nnot json at all\n')
        with pytest.raises(LedgerError) as exc:
            get_task_author_human("T-2", repo)
        assert "not valid JSON" in str(exc.value)

    def test_the_error_names_the_line_number(self, ledger):
        repo = ledger('{"task_id":"T-1"}\n{"task_id":"T-2"}\nbroken\n')
        with pytest.raises(LedgerError) as exc:
            get_task_author_human("T-9", repo)
        assert ":3" in str(exc.value), "the corrupt line number should be reported"

    def test_the_error_invokes_rl_009(self, ledger):
        repo = ledger("broken\n")
        with pytest.raises(LedgerError) as exc:
            get_task_author_human("T-1", repo)
        assert "RL-009" in str(exc.value)
        assert "stop the line" in str(exc.value)

    def test_a_row_that_is_not_an_object_is_rejected(self, ledger):
        repo = ledger('["a", "b"]\n')
        with pytest.raises(LedgerError) as exc:
            get_task_author_human("T-1", repo)
        assert "expected a JSON object" in str(exc.value)

    def test_blank_lines_are_tolerated(self, ledger):
        repo = ledger('\n{"task_id":"T-1","author_human":"Alice"}\n\n')
        assert get_task_author_human("T-1", repo) == "Alice"

    def test_corrupt_queue_file_raises_ledger_error(self, tmp_path):
        repo = tmp_path / "ledger"
        (repo / "queue").mkdir(parents=True)
        (repo / "queue" / "T-1.json").write_text("not json", encoding="utf-8")
        with pytest.raises(LedgerError) as exc:
            get_task_queue("T-1", repo)
        assert "not valid JSON" in str(exc.value)

    def test_queue_file_that_is_not_an_object_is_rejected(self, tmp_path):
        """Regression: a JSON list was returned as-is and blew up later on
        `.get("approve_role")`, far from the actual cause."""
        repo = tmp_path / "ledger"
        (repo / "queue").mkdir(parents=True)
        (repo / "queue" / "T-1.json").write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(LedgerError) as exc:
            get_task_queue("T-1", repo)
        assert "expected a JSON object" in str(exc.value)


class TestSubprocessBounds:
    """A gate that hangs is a gate that gets bypassed."""

    def test_chain_verification_timeout_raises_ledger_error(self, tmp_path, monkeypatch):
        repo = tmp_path / "ledger"
        (repo / "tools").mkdir(parents=True)
        (repo / "tools" / "verify_chain.py").touch()
        (repo / "ledger.jsonl").touch()

        def hang(*a, **k):
            raise subprocess.TimeoutExpired(cmd="verify_chain.py", timeout=60)

        monkeypatch.setattr(subprocess, "run", hang)
        with pytest.raises(LedgerError) as exc:
            get_ledger_head(repo)
        assert "timed out" in str(exc.value)

    def test_gpg_calls_are_bounded(self):
        assert GPG_TIMEOUT_S > 0

    def test_every_subprocess_run_in_the_package_is_bounded(self):
        """A new unbounded shell-out is the easiest way to reintroduce a hang.

        A call may opt out with a trailing `# unbounded:` comment stating why —
        `dx run` waits on model generation, which legitimately takes as long as
        it takes and must not be cut off by an arbitrary deadline.
        """
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "src" / "dx"
        offenders = []
        for path in sorted(src.glob("*.py")):
            text = path.read_text()
            for match in re.finditer(r"subprocess\.run\(", text):
                start = match.end() - 1  # the opening paren
                depth = 0
                end = start
                for i in range(start, len(text)):
                    depth += text[i] == "("
                    depth -= text[i] == ")"
                    if depth == 0:
                        end = i
                        break
                call = text[start : end + 1]
                if "timeout" in call or "unbounded:" in call:
                    continue
                # An opt-out may also sit on the line above the call.
                line_no = text[: match.start()].count("\n") + 1
                preceding = text.splitlines()[max(0, line_no - 6) : line_no]
                if any("unbounded:" in line for line in preceding):
                    continue
                offenders.append(f"{path.name}:{line_no}")
        assert not offenders, (
            f"subprocess.run without a timeout or a documented '# unbounded:' "
            f"reason: {offenders}"
        )

    def test_the_audit_is_not_vacuous(self):
        """If the pattern stopped matching, the audit above would pass silently."""
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "src" / "dx"
        total = sum(
            len(re.findall(r"subprocess\.run\(", p.read_text())) for p in src.glob("*.py")
        )
        assert total >= 5, f"expected several subprocess.run calls, found {total}"


class TestNoTracebackReachesTheUser:
    """The CLI turns operator-input errors into one line and exit 1."""

    def test_config_error_exits_cleanly(self, manifest, monkeypatch, capsys):
        manifest("roles:\n  - a\n")
        monkeypatch.setattr(
            sys,
            "argv",
            ["dx", "run", "T-1", "--required_role", "widget-engineer", "-m", "x", "--dry-run"],
        )
        monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/pxx")
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("❌")
        assert "Traceback" not in err
        assert "roles" in err

    def test_ledger_error_exits_cleanly(self, monkeypatch, tmp_path, capsys):
        """Regression: dx merge printed two green checkmarks and then crashed
        with a JSONDecodeError — the worst possible shape for a gate failure."""
        repo = tmp_path / "ledger"
        (repo / "queue").mkdir(parents=True)
        (repo / "queue" / "T-1.json").write_text("not json", encoding="utf-8")
        monkeypatch.setenv("DX_LEDGER_REPO", str(repo))
        monkeypatch.setattr(sys, "argv", ["dx", "merge", "T-1"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "Traceback" not in capsys.readouterr().err

    def test_a_real_bug_still_surfaces(self, monkeypatch):
        """The handler must not swallow programming errors — only the two
        operator-input families are caught."""
        monkeypatch.setattr(sys, "argv", ["dx", "roles", "list"])

        def boom(_args):
            raise ZeroDivisionError("a genuine bug")

        monkeypatch.setattr("dx.cmd_roles.cmd_roles_list", boom)
        with pytest.raises(ZeroDivisionError):
            main()
