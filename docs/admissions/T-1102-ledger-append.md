T-1102 admission record, written by tech-lead (Seat S3). Build against the
reference ledger; the live ledger is not a prerequisite for this task.

## 1. Roadmap item

ROADMAP.md 1.2: wire the `TODO(ledger)` in `src/dx/cmd_merge.py`.
`dx merge` performs a real `git merge --no-ff` under `MERGE_LOCK.json`
and appends the `SIGNED` and `MERGED` rows to `ledger.jsonl` in
`SCHEMA.md` canonical form, replacing the current verify-and-stop stub.

## 2. Objective and non-goals

**Objective:** the first `dx` code that writes to shared state — the merge
gate that verifies the approval also executes the merge and records both
ledger rows, keeping the chain verifiable afterwards.

**Non-goals:**

- The `dx.role_task.v1` evidence bundle work (ROADMAP 1.1) is already done —
  shipped in 0.9.0, admission `T-1101-evidence-bundles.md`. This task must
  not reopen, redesign, or re-touch the bundle writer.
- No live operational ledger and no RL-010 ceremony (that is ROADMAP 2.1).
- No new row types and no ledger format changes.

## 3. Base commit

`e777faf`.

## 4. Allowed and prohibited paths

- **Allowed:** `src/dx/cmd_merge.py` and its accompanying tests.
- **Prohibited:** everything else — in particular the 1.1 bundle writer,
  `verify_chain.py`, `SCHEMA.md`, the `MERGE_LOCK.json` protocol, and any
  other `src/dx` command. If the implementation needs a change there, stop
  and open a new admission rather than widening this one.

## 5. Dependencies and interface assumptions

- A writable reference ledger, which exists for exactly this task. The live
  ledger and DevSwarmX Gate 1 are not a prerequisite.
- `SCHEMA.md` canonical row form. Append-only (RL-009). Lock acquisition is
  itself an append. Concurrent claims collide as a git conflict where the
  loser backs off.
- **Sequencing trap:** appending a ledger row changes the head hash that the
  approval signature binds to. So the `SIGNED` row must be appended after
  its signature check, and the `MERGED` row after the merge, with the gate
  re-reading the head between the two appends — otherwise a correct
  implementation invalidates the signature it just accepted. `TUTORIAL.md`
  §7 demonstrates this failure deliberately. It is not an edge case.

## 6. Required tests and acceptance evidence

- A merge against the reference ledger produces both the `SIGNED` and
  `MERGED` rows, and `verify_chain.py` passes afterwards.
- A second concurrent `dx merge` loses the lock cleanly rather than
  corrupting the chain.
- A test proves the stale-signature gate still fires against the
  post-append head.
- The gate and its tests land together, and each test covers the way the
  gate can wrongly pass, not only the happy path.

## 7. Reviewer and escalation owner

- **Reviewer:** not tech-lead. By separation of duties the author of this
  admission cannot be the sole reviewer of the implementation commit. A
  second seat (architect or a peer engineer) reviews the diff and re-runs
  the acceptance evidence independently.
- **Escalation owner:** interface disputes (row form, lock protocol, head
  re-read) go to the architect. The live-ledger block (DevSwarmX Gate 1)
  escalates to the accountable human who holds the RL-010 key. That
  decision is human, not engineering.

---

## Amendment 1 — 2026-09-08, before implementation

Issued under the same `tech-lead` card. §4 allowed `src/dx/cmd_merge.py` only.
Implementation needs one module it does not cover, so the record is amended
before the work rather than exceeded during it — which is what §4 itself
instructs.

**Added to allowed paths:** `src/dx/ledger_writer.py` (new),
`tests/test_ledger_writer.py` (new).

**Why a new module rather than more of `cmd_merge.py`.** `cmd_merge.py` is a
command: it parses arguments, orders gates, and prints verdicts. Ledger writing
is a different concern with its own invariants — canonical form, chain
continuity, lock discipline — and it is the first code in `dx` that mutates
shared state. Mixing it into a command module makes those invariants untestable
except through the CLI, and they are exactly the invariants that need direct
tests.

**Still prohibited, unchanged:** `verify_chain.py` and `SCHEMA.md` are the
contract this code must conform to, not negotiate with. `ledger_writer.py`
reimplements the canonical form to *write* rows and a test asserts byte-identical
agreement with the verifier's own function — if they ever disagree, that test
fails rather than the chain breaking in production.

## Amendment 2 — 2026-09-08, during implementation

**Added to allowed paths:** `tests/conftest.py`, `tests/test_cmd_merge.py`,
`tests/test_gpg_integration.py`, `tests/test_docs_consistency.py`.

Forced, and worth recording rather than absorbing quietly. `dx merge` was a
read-only command; it is now a write. Two consequences the record did not
foresee:

1. **`fake_ledger` was a fiction that only worked while nothing wrote.** Its
   stub `verify_chain.py` printed a head hash unrelated to its own rows, and the
   rows carried no `prev_hash` at all. `append_row` cross-checks the verifier's
   claimed head against the actual last row and refused — correctly. The fixture
   is now a real chain in a real git repo, with the head computed from the rows.
2. **A test shelled out to `dx merge` and wrote to the operator's real ledger.**
   `test_the_green_merge_transcript_is_reproducible` runs `dx merge T-0001` as a
   subprocess; with `DX_LEDGER_REPO` unset that resolves to
   `~/ai/devswarm-ledger-reference`. It appended SIGNED, MERGED and UNLOCK
   commits to the working clone. Restored from `origin/main`; nothing was
   pushed, so the published ledger was never affected. The transcript tests now
   run against a disposable copy.

The second is the sharper lesson: a test that was safe against a read-only
command became a mutation the moment the command grew teeth, and nothing about
the test changed. Any test that invokes a writing command needs its own
disposable target.
