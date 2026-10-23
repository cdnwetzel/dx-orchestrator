"""`dx roles list` / `dx roles validate` output contracts."""
import json

import pytest

from dx.cli import build_parser


def _roles(*argv):
    args = build_parser().parse_args(["roles", *argv])
    args.func(args)


def test_list_renders_a_table(capsys):
    _roles("list")
    out = capsys.readouterr().out
    assert "Slug" in out and "Fit" in out and "Seat" in out
    assert "widget-engineer" in out
    assert "rotating-reviewer" in out


def test_list_column_width_fits_the_longest_value(capsys):
    """Regression: the seat column was hardcoded to 14 and overflowed on real
    compound seats like 'Rotation (S3/S4/S7)'.
    """
    _roles("list")
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "rotating-reviewer" in ln]
    assert lines
    assert "Rotation (S3/S4/S7)" in lines[0]
    # The Anchored column must still be a separate, non-overlapping field.
    assert lines[0].rstrip().endswith("Rotation (S3/S4/S7)")


def test_anchored_rows_are_marked(capsys):
    _roles("list")
    row = next(ln for ln in capsys.readouterr().out.splitlines() if "oracle-sme" in ln)
    assert row.rstrip().endswith("YES")


def test_filter_by_fit(capsys):
    _roles("list", "--fit", "High")
    out = capsys.readouterr().out
    assert "widget-engineer" in out
    assert "oracle-sme" not in out


def test_filter_anchored(capsys):
    _roles("list", "--anchored")
    out = capsys.readouterr().out
    assert "oracle-sme" in out
    assert "widget-engineer" not in out


def test_json_output_is_valid_json(capsys):
    _roles("list", "--json")
    payload = json.loads(capsys.readouterr().out)
    assert {c["slug"] for c in payload} >= {"widget-engineer", "oracle-sme"}
    assert all({"slug", "fit", "seat", "anchored"} <= set(c) for c in payload)


def test_single_card_json_carries_the_enforced_sections(capsys):
    _roles("list", "--slug", "widget-engineer", "--json")
    card = json.loads(capsys.readouterr().out)
    assert card["slug"] == "widget-engineer"
    assert card["must_not"]
    assert card["prohibited_patterns"]


def test_unknown_slug_exits_one(capsys):
    with pytest.raises(SystemExit) as exc:
        _roles("list", "--slug", "nope")
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_empty_filter_result_is_not_an_error(capsys):
    _roles("list", "--seat", "S999")
    assert "no roles match" in capsys.readouterr().out


def test_validate_fails_on_the_malformed_fixture(capsys):
    with pytest.raises(SystemExit) as exc:
        _roles("validate")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "malformed-role" in out


def test_validate_passes_on_a_clean_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path))
    (tmp_path / "clean-role.md").write_text(
        "# Clean\n\n**Agent fit:** High · **9-person seat:** S1\n\n"
        "## Mandate\n\nA mandate long enough to satisfy validation.\n\n"
        "## Must not (separation of duties)\n\n- Do not self-approve\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        _roles("validate")
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "Fit distribution" in out


def test_explicit_path_overrides_the_env_var(tmp_path, capsys):
    (tmp_path / "solo-role.md").write_text(
        "# Solo\n\n**Agent fit:** High · **9-person seat:** S1\n\n## Mandate\n\nSolo.\n",
        encoding="utf-8",
    )
    _roles("list", "--path", str(tmp_path))
    out = capsys.readouterr().out
    assert "solo-role" in out
    assert "widget-engineer" not in out


def test_missing_roles_directory_exits_one(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path / "absent"))
    with pytest.raises(SystemExit) as exc:
        _roles("list")
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err
