"""A released lock is not a held lock."""
import json, pathlib, subprocess, pytest
from dx.ledger_writer import MergeLock, LedgerWriteError

def _lock(tmp_path, payload, git=False):
    if git:
        # acquire() commits the lock, so the happy path needs a real repo.
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "queue").mkdir(parents=True, exist_ok=True)
    (tmp_path / "queue" / "MERGE_LOCK.json").write_text(json.dumps(payload))
    return MergeLock(ledger_repo=tmp_path, task_id="T-9999")

def test_a_released_lock_does_not_block(tmp_path):
    """THE ONE THAT MATTERED. dx's release() deletes the file, but the ledger
    predates dx and its tooling marks state instead -- devswarm-ledger has
    carried {"state": "released", "task_id": "T-0004"} since August. Reading
    only task_id meant the FIRST successful merge locked the ledger forever:
    every later task saw a stale holder and refused."""
    _lock(tmp_path, {"state": "released", "task_id": "T-0004"}, git=True).acquire()

def test_a_held_lock_still_blocks(tmp_path):
    with pytest.raises(LedgerWriteError, match="held by"):
        _lock(tmp_path, {"task_id": "T-0004"}).acquire()

def test_an_explicitly_held_lock_still_blocks(tmp_path):
    with pytest.raises(LedgerWriteError, match="held by"):
        _lock(tmp_path, {"state": "held", "task_id": "T-0004"}).acquire()

def test_our_own_lock_is_reentrant(tmp_path):
    _lock(tmp_path, {"task_id": "T-9999"}, git=True).acquire()


def test_a_non_git_ledger_says_what_is_wrong(tmp_path):
    """Not auto-init. The ledger's durability IS its git history, so creating
    an empty repo would make "no history" and "valid but empty" the same thing
    -- silently, mid-merge. And the likeliest cause is a path pointing
    somewhere wrong, which auto-init would turn into a plausible ledger.

    So the requirement is a refusal that names the cause and the two fixes,
    rather than git's own "fatal: not a git repository", which says neither.
    """
    with pytest.raises(LedgerWriteError) as exc:
        _lock(tmp_path, {"state": "released", "task_id": "T-0004"}).acquire()
    msg = str(exc.value)
    assert "not a git repository" in msg
    assert "DX_LEDGER_REPO" in msg, "must name the setting that is probably wrong"
    assert "git init" in msg, "must say what to do if it really is a new ledger"
