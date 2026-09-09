"""`dx doctor` — the first command anyone runs, and the one that decides whether
an install is usable. Its exit code is the contract: 0 means core checks passed.
"""
import socket

import pytest

from dx.cli import build_parser


@pytest.fixture
def all_green(monkeypatch, tmp_path):
    """Make every core check pass without touching the real environment.

    Everything is steered through the documented env overrides — if a check
    cannot be redirected this way, that is itself a portability bug.
    """
    monkeypatch.setattr("dx.cmd_doctor._find_on_path", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr("dx.cmd_doctor.subprocess.run", lambda *a, **k: _Ok())
    monkeypatch.setattr("dx.cmd_doctor._check_import", lambda module, label: True)

    psop = tmp_path / "psoperator" / "examples"
    psop.mkdir(parents=True)
    (psop / "run_agent.py").touch()
    monkeypatch.setenv("PSOPERATOR_REPO", str(tmp_path / "psoperator"))

    ledger = tmp_path / "ledger" / "tools"
    ledger.mkdir(parents=True)
    (ledger / "verify_chain.py").touch()
    monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "ledger"))
    return tmp_path


class _Ok:
    returncode = 0
    stdout = b""
    stderr = b""


def _doctor(*argv):
    args = build_parser().parse_args(["doctor", *argv])
    args.func(args)


def test_exits_zero_when_everything_passes(all_green, capsys):
    with pytest.raises(SystemExit) as exc:
        _doctor("--no-network")
    out = capsys.readouterr().out
    assert exc.value.code == 0, out
    assert "All core checks passed" in out


def test_exits_one_when_a_core_check_fails(all_green, monkeypatch, capsys):
    """pxx missing is a core failure — dx run cannot work without it."""
    monkeypatch.setattr("dx.cmd_doctor._find_on_path", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        _doctor("--no-network")
    assert exc.value.code == 1
    assert "Some core checks failed" in capsys.readouterr().out


def test_missing_role_cards_is_a_core_failure_and_names_the_override(
    all_green, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path / "absent"))
    with pytest.raises(SystemExit) as exc:
        _doctor("--no-network")
    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "Role cards missing" in out
    assert "DX_ROLES_PATH" in out


def test_reports_the_resolved_manifest_path_not_a_hardcoded_one(
    all_green, manifest_path, capsys
):
    """Regression: doctor printed ~/.config/dx/... regardless of DX_CONFIG, so it
    could report a file it had not actually read."""
    with pytest.raises(SystemExit):
        _doctor("--no-network")
    assert str(manifest_path) in capsys.readouterr().out


def test_missing_manifest_is_a_core_failure(all_green, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DX_CONFIG", str(tmp_path / "absent.yml"))
    with pytest.raises(SystemExit) as exc:
        _doctor("--no-network")
    assert exc.value.code == 1
    assert "Hardware manifest missing" in capsys.readouterr().out


def test_unparseable_manifest_is_a_core_failure(all_green, monkeypatch, tmp_path, capsys):
    bad = tmp_path / "bad.yml"
    bad.write_text("roles: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("DX_CONFIG", str(bad))
    with pytest.raises(SystemExit) as exc:
        _doctor("--no-network")
    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "invalid YAML" in out


@pytest.mark.parametrize(
    "manifest,offender",
    [
        ("roles:\n  - backend-engineer\n", "roles"),
        ("roles: nope\n", "roles"),
        ("roles:\n  backend-engineer: http://host:1\n", "roles.backend-engineer"),
        ("roles:\n  default: http://host:1\n", "roles.default"),
        ("gui_verification:\n  - a\n", "gui_verification"),
        ("psoperator:\n  - a\n", "psoperator"),
        ("- a\n- b\n", "the manifest"),
    ],
)
def test_wrong_shaped_manifest_is_a_core_failure(
    all_green, monkeypatch, tmp_path, capsys, manifest, offender
):
    """Regression: doctor validated YAML *syntax* only, so a manifest that
    parsed but had the wrong shape — `roles:` written as a list, say — was
    reported as a healthy install right up until the first `dx run` failed.
    A green doctor has to mean the config is actually usable.
    """
    bad = tmp_path / "shape.yml"
    bad.write_text(manifest, encoding="utf-8")
    monkeypatch.setenv("DX_CONFIG", str(bad))
    with pytest.raises(SystemExit) as exc:
        _doctor("--no-network")
    out = capsys.readouterr().out
    assert exc.value.code == 1, out
    assert f"`{offender}`" in out or offender in out
    assert "Some core checks failed" in out


def test_well_shaped_manifest_reports_shape_not_just_syntax(all_green, capsys):
    with pytest.raises(SystemExit) as exc:
        _doctor("--no-network")
    out = capsys.readouterr().out
    assert exc.value.code == 0, out
    assert "expected shape" in out


def test_missing_ledger_is_a_warning_not_a_failure(all_green, monkeypatch, tmp_path, capsys):
    """dx run and dx roles work fine without the ledger; only dx merge needs it."""
    monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "no-ledger"))
    with pytest.raises(SystemExit) as exc:
        _doctor("--no-network")
    out = capsys.readouterr().out
    assert exc.value.code == 0, out
    assert "devswarm-ledger not found" in out


class TestNetworkProbes:
    def test_no_network_skips_probes(self, all_green, monkeypatch, capsys):
        monkeypatch.setattr(
            socket, "create_connection",
            lambda *a, **k: pytest.fail("network probe ran under --no-network"),
        )
        with pytest.raises(SystemExit):
            _doctor("--no-network")
        assert "Network checks" not in capsys.readouterr().out

    def test_probes_are_derived_from_the_manifest(self, all_green, monkeypatch, capsys):
        seen = []

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_connect(addr, timeout=None):
            seen.append(addr)
            return _Conn()

        monkeypatch.setattr(socket, "create_connection", fake_connect)
        with pytest.raises(SystemExit):
            _doctor()

        out = capsys.readouterr().out
        assert "Network checks" in out
        # Every host in tests/fixtures/manifest.yml, and nothing invented.
        assert ("vllm.invalid", 8007) in seen
        assert ("ollama.invalid", 11434) in seen
        assert ("vlm.invalid", 11434) in seen
        assert all(host.endswith(".invalid") for host, _ in seen)

    def test_duplicate_endpoints_are_probed_once(self, all_green, monkeypatch, capsys):
        """Two roles pointing at one box should not produce two probes."""
        seen = []

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(
            socket, "create_connection",
            lambda addr, timeout=None: (seen.append(addr), _Conn())[1],
        )
        with pytest.raises(SystemExit):
            _doctor()
        assert len(seen) == len(set(seen)), f"duplicate probes: {seen}"

    def test_unreachable_endpoint_is_not_a_core_failure(self, all_green, monkeypatch, capsys):
        """A lab that is off must not make doctor claim the install is broken."""
        monkeypatch.setattr(
            socket, "create_connection",
            lambda *a, **k: (_ for _ in ()).throw(OSError("unreachable")),
        )
        with pytest.raises(SystemExit) as exc:
            _doctor()
        out = capsys.readouterr().out
        assert exc.value.code == 0, out
        assert "not reachable" in out
        assert "All core checks passed" in out


def test_psoperator_path_is_overridable(all_green, monkeypatch, tmp_path, capsys):
    """Regression: doctor hard-coded ~/ai/psoperator while psoperator_client
    honoured PSOPERATOR_REPO, so the two disagreed about the same install."""
    monkeypatch.setenv("PSOPERATOR_REPO", str(tmp_path / "elsewhere"))
    with pytest.raises(SystemExit) as exc:
        _doctor("--no-network")
    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "run_agent script missing" in out
    assert str(tmp_path / "elsewhere") in out
    assert "PSOPERATOR_REPO" in out


class TestObserverHealthProbe:
    """§3.1: `dx doctor` should see the observer without manual intervention —
    but only when the observer is configured for use (the attestation key path
    is set), so a plain install is not nagged about a service it does not run."""

    def test_healthy_observer_is_reported(self, all_green, monkeypatch, capsys):
        monkeypatch.setenv("PSOPERATOR_OBSERVER_ATTESTATION_KEY_PATH", "/x/observer.key")
        monkeypatch.setenv("PSOPERATOR_OBSERVER_PORT", "8764")
        monkeypatch.setattr("dx.cmd_doctor._observer_health", lambda h, p: "epoch abc123…, key k")
        with pytest.raises(SystemExit):
            _doctor()
        out = capsys.readouterr().out
        assert "observer → 127.0.0.1:8764 healthy" in out

    def test_unhealthy_observer_warns_but_does_not_fail(self, all_green, monkeypatch, capsys):
        monkeypatch.setenv("PSOPERATOR_OBSERVER_ATTESTATION_KEY_PATH", "/x/observer.key")
        def boom(h, p):
            raise RuntimeError("observer unavailable: connection refused")
        monkeypatch.setattr("dx.cmd_doctor._observer_health", boom)
        with pytest.raises(SystemExit) as exc:
            _doctor()
        out = capsys.readouterr().out
        assert exc.value.code == 0, out  # non-critical: still all-green
        assert "observer" in out and "not healthy" in out

    def test_unconfigured_observer_is_not_probed(self, all_green, monkeypatch, capsys):
        monkeypatch.delenv("PSOPERATOR_OBSERVER_ATTESTATION_KEY_PATH", raising=False)
        called = []
        monkeypatch.setattr("dx.cmd_doctor._observer_health", lambda h, p: called.append(1) or "x")
        with pytest.raises(SystemExit):
            _doctor()
        assert not called
        assert "observer →" not in capsys.readouterr().out
