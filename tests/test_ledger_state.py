"""dx.ledger_state — one reading of a task's rows.

Four programs each had their own idea of "closed" on 2026-09-21, and the one
that could sign used the weakest. These tests pin the shared reading, mostly
through shapes taken from the real ledger that day.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dx.ledger_state import (
    LedgerStateError,
    candidate_for_merge,
    current_state,
    is_reference_ledger,
    task_rows,
    why_not_signable,
)

SHA = "c000c0cab045d47f1c27e35d79a72cb504f77159"
SHA2 = "d" * 40


def _rows(*actions: str | tuple[str, dict]) -> list[dict]:
    out = []
    for a in actions:
        if isinstance(a, tuple):
            name, extra = a
        else:
            name, extra = a, {}
        row = {"task_id": "T-X", "action": name, "author_human": "Ada Author",
               "evidence": f"mode=TEST {name}"}
        row.update(extra)
        out.append(row)
    return out


EXEC = ("EXECUTED", {"sha": SHA})


class TestRealShapes:
    """Every sequence here existed on the real ledger on 2026-09-21."""

    @pytest.mark.parametrize("seq, action, signable, supersedable, terminal", [
        # T-0009..T-0018: a bare admission, nothing ran
        (("ADMITTED",), "ADMITTED", False, True, False),
        # T-0032: ran, committed, awaiting a second human
        (("ADMITTED", EXEC), "EXECUTED", True, True, False),
        # T-0019: sent back twice
        (("ADMITTED", "REDLINE", "REDLINE"), "REDLINE", False, True, False),
        # T-0020..T-0024: died on a refusal
        (("ADMITTED", "INCOMPLETE"), "INCOMPLETE", False, True, False),
        # T-0008
        (("ADMITTED", "ABANDONED"), "ABANDONED", False, False, True),
        # T-0003: the whole path (EVIDENCE is record-only)
        (("ADMITTED", "EVIDENCE", "SIGNED", "MERGED"), "MERGED", False, False, True),
        # T-0007: signed, never merged
        (("ADMITTED", "INCOMPLETE", "ADMITTED", "EVIDENCE", "SIGNED"), "SIGNED", False, False, False),
    ])
    def test_shape(self, seq, action, signable, supersedable, terminal):
        s = current_state(_rows(*seq), "T-X")
        assert s.action == action
        assert s.is_signable is signable
        assert s.supersedable is supersedable
        assert s.terminal is terminal

    def test_T_0002_redline_correction_readmit_signed_merged(self):
        s = current_state(_rows("REDLINE", "CORRECTION", "ADMITTED", "EVIDENCE",
                                "SIGNED", "MERGED"), "T-0002")
        assert s.action == "MERGED"
        assert s.corrections == 1
        assert s.terminal


class TestTheReading:
    def test_a_redline_withholds(self):
        s = current_state(_rows("ADMITTED", EXEC, "REDLINE"))
        assert s.withheld
        assert "REDLINE" in why_not_signable(s)
        assert "objection" in why_not_signable(s)

    def test_re_admission_reopens_and_clears_the_candidate(self):
        """A rework's re-admission: the old candidate was the old admission's."""
        s = current_state(_rows("ADMITTED", EXEC, "REDLINE", "ADMITTED"))
        assert s.action == "ADMITTED"
        assert not s.withheld
        assert s.candidate_sha is None
        assert "no EXECUTED row" in why_not_signable(s)

    def test_a_new_candidate_after_re_admission_is_signable(self):
        s = current_state(_rows("ADMITTED", EXEC, "REDLINE", "ADMITTED",
                                ("EXECUTED", {"sha": SHA2})))
        assert s.candidate_sha == SHA2
        assert s.is_signable

    def test_reviewed_after_redline_is_the_reviewer_withdrawing(self):
        """Only a non-author may record REVIEWED; last status wins, on purpose."""
        s = current_state(_rows("ADMITTED", EXEC, "REDLINE", "REVIEWED"))
        assert not s.withheld
        assert s.is_signable

    def test_escalated_withholds(self):
        s = current_state(_rows("ADMITTED", EXEC, "ESCALATED"))
        assert s.withheld and not s.is_signable

    def test_an_executed_row_without_a_sha_is_no_candidate(self):
        """The fixture shape before 2026-09-21 — and a real risk if a writer
        ever records EXECUTED without the commit that proves it."""
        s = current_state(_rows("ADMITTED", "EXECUTED"))
        assert s.action == "EXECUTED"
        assert not s.executed
        assert "no EXECUTED row with a commit" in why_not_signable(s)
        # ...but it IS a row. A recorder that ignored it would append a second
        # EXECUTED and rewrite history.
        assert s.execution_recorded

    def test_re_admission_clears_the_recorded_execution_too(self):
        s = current_state(_rows("ADMITTED", EXEC, "INCOMPLETE", "ADMITTED"))
        assert not s.execution_recorded

    def test_correction_alone_changes_nothing(self):
        before = current_state(_rows("ADMITTED", EXEC))
        after = current_state(_rows("ADMITTED", EXEC, "CORRECTION"))
        assert after.action == before.action
        assert after.is_signable == before.is_signable
        assert after.corrections == 1

    def test_signed_is_not_supersedable(self):
        """Signed work is corrected, not superseded; a signature stays in the record."""
        assert not current_state(_rows("ADMITTED", EXEC, "SIGNED")).supersedable

    def test_signed_is_not_signable_again(self):
        assert "already SIGNED" in why_not_signable(
            current_state(_rows("ADMITTED", EXEC, "SIGNED")))

    def test_no_rows(self):
        s = current_state([], "T-NONE")
        assert s.action == ""
        assert not s.is_open
        assert "no rows" in why_not_signable(s)

    def test_the_reason_names_who_withheld(self):
        rows = _rows("ADMITTED", EXEC, ("REDLINE", {"author_human": "Frank Diaz"}))
        assert "Frank Diaz" in why_not_signable(current_state(rows))


class TestCandidateForMerge:
    def _state(self):
        return current_state(_rows("ADMITTED", EXEC))

    def test_queue_agreeing_with_the_ledger_yields_the_sha(self):
        assert candidate_for_merge({"sha": SHA}, self._state()) == SHA

    def test_the_legacy_field_name_is_refused_and_named(self):
        """What every bridge task's queue file looked like on 2026-09-21."""
        with pytest.raises(LedgerStateError, match="executed_sha"):
            candidate_for_merge({"executed_sha": SHA}, self._state())

    def test_a_queue_with_no_sha_is_refused(self):
        with pytest.raises(LedgerStateError, match="no 'sha'"):
            candidate_for_merge({}, self._state())

    def test_a_queue_disagreeing_with_the_ledger_is_refused(self):
        """The ledger is the record; a queue that says otherwise is a question."""
        with pytest.raises(LedgerStateError, match="ledger is the record"):
            candidate_for_merge({"sha": SHA2}, self._state())

    def test_an_unsignable_task_is_refused_before_the_queue_is_read(self):
        s = current_state(_rows("ADMITTED", EXEC, "REDLINE"))
        with pytest.raises(LedgerStateError, match="REDLINE"):
            candidate_for_merge({"sha": SHA}, s)


class TestTaskRows:
    def test_only_that_task_in_order(self, tmp_path: Path):
        led = tmp_path
        (led / "ledger.jsonl").write_text(
            '{"task_id":"T-1","action":"ADMITTED"}\n'
            '{"task_id":"T-2","action":"ADMITTED"}\n'
            '\n'
            f'{{"task_id":"T-1","action":"EXECUTED","sha":"{SHA}"}}\n')
        rows = task_rows(led, "T-1")
        assert [r["action"] for r in rows] == ["ADMITTED", "EXECUTED"]

    def test_a_corrupt_line_stops_the_line(self, tmp_path: Path):
        (tmp_path / "ledger.jsonl").write_text('{"task_id":"T-1"}\nnot json\n')
        with pytest.raises(LedgerStateError, match="RL-009"):
            task_rows(tmp_path, "T-1")

    def test_a_missing_ledger_is_named(self, tmp_path: Path):
        with pytest.raises(LedgerStateError, match="no ledger"):
            task_rows(tmp_path / "absent", "T-1")


class TestReferenceLedger:
    def test_the_reference_ledger_is_recognised_by_name(self, tmp_path: Path):
        assert is_reference_ledger(tmp_path / "devswarm-ledger-reference")
        assert not is_reference_ledger(tmp_path / "devswarm-ledger")

    def test_through_a_symlink(self, tmp_path: Path):
        real = tmp_path / "devswarm-ledger-reference"
        real.mkdir()
        link = tmp_path / "ledger"
        link.symlink_to(real)
        assert is_reference_ledger(link)
