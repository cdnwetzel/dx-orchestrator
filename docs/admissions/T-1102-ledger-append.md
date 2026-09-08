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
