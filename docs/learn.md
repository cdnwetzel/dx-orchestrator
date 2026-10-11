# dx learn — continuous improvement from past iterations

`dx learn` reads what already happened across the stack, ranks what is worth
changing, and stops. A named human accepts or rejects each proposal. An
accepted proposal becomes a dx task spec that goes through `dx run` and a
signed `dx merge` like any other change. Once that task merges, `dx learn
efficacy` compares the metric fixed at proposal time before and after the
merge. That is an *observed* change, not proof the change caused it (see
"Before/after comparison" below). Only strong evidence of no meaningful
improvement comes back as a proposal to review revising or reverting.

```
 pxx runs ─┐                    ┌─ PROPOSED ──accept (human)──► dx task spec ─► dx run ─► dx merge (signed)
 ledger ───┤  verify ─► learn ─►│      │                                                      │ MERGED row
 psoperator┤  (fail     (pure,  │      └─reject (human)─► stays down until evidence ≥ 2×      ▼
 reviews ──┘   closed)  seeded) └─ model routing ─► pxx inbox (pxx triage decides)   efficacy: observed
                                                                                     before/after change,
                                                                                     not causal; only strong
                                                                                     evidence of no meaningful
                                                                                     improvement ─► "review:
                                                                                     revise or revert?"
```

## Why it lives in dx

pxx already has a propose-only improvement plane: `pxx improve` handles mining,
triage, candidates, held-out evaluation, promotion and canary channels.
`dx learn` does not replace it. dx is the only layer that sees all four
records, and the most valuable signal is the join between them. Three
examples:

- A pxx run that reported `COMPLETED` on a task a human then marked `INCOMPLETE`.
- A failing test that recurs across tasks.
- A review lane that rarely answers.

Anything pxx can carry is handed to pxx's inbox, so pxx's human gates stay the
only path to its production config. Today that means only model routing.

## Sources, and how far each is trusted

| Source | Read from | Trust rule |
| --- | --- | --- |
| pxx runs | `<state_dir>/runs/<id>/` | No chain exists. One `pxx loop` (what `dx run` drives) is one attempt. Its `*-loop-*` directory holds the loop's true terminal code and gates, and model/task/project come from its `session_start` events. Its round directories are folded in and never counted alone, because a round can say `COMPLETED` inside a loop that ended `ROUND_CAP`. A quarantined loop still claims its rounds. A standalone `pxx run` session is one attempt. Malformed directories are quarantined and named; ones without `outcome.json` are counted as in flight. |
| devswarm ledger | `DX_LEDGER_REPO` | `get_ledger_head()` (the ledger's own `verify_chain.py`) must pass first. Rows are then re-hashed while reading, and only rows up to that verified head are used; anything appended since is ignored. A broken chain stops the pass and leaves the queue unchanged. The reference ledger is refused without `--allow-reference-ledger`, whether recognized by directory name or by its published genesis row. A `CORRECTION` sets aside the task of the row whose hash it names; failing that, its own task. If it can be attributed to neither, the pass stops. `run=`/`scope=` are parsed with quoted text (the model-written `subject="…"`) removed; duplicates set the task aside. |
| psoperator audit | `--psoperator-audit` / `DX_LEARN_PSOPERATOR_AUDIT` | Re-verified with psoperator's own rule (`prev_hash`, content hash, `seq`). Any break stops the pass. `kill-switched` rows are ignored because drills write identical rows. |
| review manifests | `--review-dir <repo>/review` (repeatable) | A row with an unknown `result` is quarantined. `EXCLUDED` never counts for or against a lane. Timestamps come from the manifest's git commit. |

The join is the `run=<pxx run id>` key that `dx run` writes into `EXECUTED`
evidence. A linked pxx attempt takes the dx task id and its `scope=` as the
task class.

## The algorithm

Every measure is a *bad-event rate*, defined once in
`learn_engine.observations()`. Detection and efficacy both use that same
definition, so the yardstick is pre-registered rather than chosen after the
data arrives.

Each metric also names the source it reads (`pxx`, `ledger`, `operator` or
`review`). If that source was not read in a pass, the metric is unavailable.
It is never quietly measured against another source, where it would read as
zero.

`gate_fail_rate` uses pxx's own gate decision (`allowed: false`). For the
`tests` gate that means new failures beyond baseline, not "tests did not
pass". A suite that never passes shows up in the loop's terminal code
instead.

**Rates.** Every rate is a Beta(1,1)-Binomial posterior. Observations decay
with a 30-day half-life, so old evidence fades instead of being cut off at a
window edge.

**Comparisons.** Comparisons are P(A > B) by Monte Carlo with a fixed seed, so
the same records always produce the same queue.

**Signatures.** A failure signature is the sha256 of
`(where, error class, normalized message)`. Normalization removes paths, line
numbers, UUIDs, timestamps, git SHAs, quoted literals and numbers. One
recurring defect therefore gets one signature, whichever file or value it
surfaced on.

| Kind | Fires when (defaults) | Metric it will be judged by |
| --- | --- | --- |
| `recurring_failure` | pxx terminal signature or newly failing test; decayed weight ≥ 3 across ≥ 2 tasks. Records which later attempt fixed it, and on which model. | `signature_rate` |
| `variant_preference` | Two or more models in a task class each have ≥ 8 effective runs, and the best has a lower failure rate than another with P ≥ 0.95. | `class_failure_rate` |
| `gate_regression` | A pxx gate's fail rate over the last 14 days is ≥ 10 points above the prior 90 days, with P ≥ 0.90. | `gate_fail_rate` |
| `human_rejection_theme` | One shape of `REDLINE`/`INCOMPLETE`/`ABANDONED`/`ESCALATED` is ≥ 25% of negative rows (decayed weight ≥ 2). Lists tasks where pxx said `COMPLETED` first. | `signature_rate` |
| `reviewer_lane` | P(availability < 75%) ≥ 0.90. The 75% bar is pxx's own reviewer-calibration bar. | `lane_unavailability` |
| `operator_failure` | One psoperator denial, stale, target or execution-failure shape recurs (weight ≥ 3). | `signature_rate` |
| `adoption_no_improvement` | Status `no-meaningful-improvement` (strong evidence of no meaningful *observed* improvement; see "Before/after comparison"). | the adoption's metric |

**Ranking.** Recommendations are ranked by evidence weight × where the problem
surfaced. A problem a human catches in the ledger weighs 4. A problem caught by
psoperator weighs 3. A problem caught by a pxx gate weighs 1.5–2.

**What the kinds cannot do.** The kinds are a closed list. None of them relaxes
a gate, budget, risk tier or freshness window, and a test pins that list.
Every generated spec's non-goals say that moving the metric by loosening a
control counts as a failure of the task.

## The queue

| From | Event | To |
| --- | --- | --- |
| new | a pass finds evidence | `PROPOSED` |
| `PROPOSED` | a pass no longer finds it | `STALE` (can come back) |
| `PROPOSED`/`STALE` | `accept --by <human>` | `ACCEPTED` + an adoption + a task spec |
| `PROPOSED`/`STALE` | `reject --by <human> --note` | `REJECTED`; re-raised only when evidence ≥ 2× its weight at rejection, carrying the earlier decision |
| adoption | `link --task T-…` | efficacy starts at that task's `MERGED` row, never at acceptance |
| adoption | `retire` | `RETIRED` |

**Separation of duties.** dx learn authors every proposal, so `--by` must name
a human.
- A name made only of tool or model words is refused. Examples: `dx.run`,
  `Claude Code`, `claude-opus-5-5`, `pxx`, `dx <dx@…>`.
- The OS account that ran the command is recorded beside the name.
- This is a guard against an automated caller, not authentication. The
  authority that changes software is still the signed `dx merge`.

**Concurrency (revisions).** Recommendations, adoptions and forward records
each carry a `revision`. Every change is one transaction (`learn_store.Tx`)
listing the records it writes, each at the revision it was read. The store
accepts the transaction only if every one of those is still the stored
revision, then bumps them; otherwise nothing is written.
- Status alone was not enough: two `link`s on an ACCEPTED recommendation
  both saw ACCEPTED. Each now writes the adoption at the revision it read,
  so the second one is refused.
- Every command that changes state takes the store's mutation lock, re-reads
  what it decides on, and commits once.
- Re-reading inside the lock serializes two commands; it does not make the
  second one wrong. So `link` is first-writer-wins: a second link is refused
  unless it names the task it replaces (`--replace-task`).
- Every decision can also be bound to exactly what the human looked at.
  `show` and `queue` print a seen-token (the recommendation and adoption
  revisions). `--seen <token>` on `accept`/`reject`/`link`/`retire` refuses
  the decision if anything changed since. Without `--seen`, a decision applies
  to the state at the moment it is recorded.
- A pass writes back with "skip conflicts": a record decided while the pass
  ran is left alone and reported.

**Audit.** The audit entry is part of the same transaction as the state it
describes.
- The JSON store keeps a hash-chained `audit.jsonl`.
- MSSQL uses an append-only, trigger-guarded table carrying the same chain
  (`EntryJson`, `PrevHash`, `Hash`).
- The chain is verified before every transaction, and a chain that does not
  verify blocks further decisions.
- `queue` prints the chain's state first. Forwarding to pxx is audited too
  (`forward-intent`, `forward-outcome`, `forward-reconcile`, actor `dx learn`).

**Ordering.** `queue` prints earlier adoptions before new proposals. This
follows the scrum-master card: the last set of retro actions is checked before
new ones are added.

## Before/after comparison (`dx learn efficacy`)

For each adoption, dx learn splits the metric fixed at proposal time at the
first `MERGED` row of the implementing task at or after acceptance. It then
compares the 60 days before with the 60 days after. The "after" window is
bounded so that later, unrelated changes do not leak into it.

**What this shows and what it does not.** It shows how the rate changed
across the merge. It does not show that the merge caused the change. Other
things move the same numbers:

- **Regression to the mean.** A proposal is raised *because* a rate looked
  high, so that rate tends to fall afterwards anyway. "Improvement observed"
  is therefore biased toward looking better than the change deserves.
- **Cohort change.** Different tasks, task classes or models before and
  after the merge are not comparable populations.
- **Concurrent changes.** A pxx upgrade, a prompt change, another dx task,
  or a change in the test suite itself.
- **Another adoption on the same metric** merging inside the window. The two
  cannot be told apart.
- **Small samples**, where the prior alone can make equal rates look
  different.

**What dx learn does about it.** It cannot remove these effects, so it
refuses verdicts they could explain:

- Every measured row carries a caveat saying it is not a causal estimate.
- A shift in the model mix or task-class mix across the merge (total
  variation > 0.30) voids both verdicts: `inconclusive (confounded)`. Only
  pxx attempts carry these descriptors; for ledger, operator and review
  metrics the cohort is not characterized, and that is a known gap.
- Another adoption on the same metric — active or retired — merging anywhere
  in the 60 days before or after also voids both verdicts.
- The negative verdict, the only one that proposes revising or reverting,
  requires much more than "did not improve".

| Status | Meaning |
| --- | --- |
| `awaiting-task` / `awaiting-merge` | No implementing task linked yet, or the task has no MERGED row at or after acceptance. |
| `merged-before-adoption` | The linked task merged before the proposal was accepted; it cannot be the change. |
| `source-not-read` | The metric's source was not read this pass. |
| `measuring (before a/10, after b/10)` | Fewer than 10 observations on either side of the merge. |
| `no-baseline-problem` | Nothing was failing before the merge, so there is nothing to credit. |
| `inconclusive (confounded)` | Cohort shift or an overlapping adoption; see the caveats. |
| `improvement-observed` | P(rate fell) ≥ 0.90 and the raw rate fell. Observed, not attributed. |
| `no-meaningful-improvement` | ≥ 30 observations on each side, and P ≥ 0.95 that any improvement is below 25% of the before-rate. The bar is relative on purpose: an absolute floor would, at a low baseline, call even a halved rate "no improvement". Raised as `adoption_no_improvement`: a prompt to review whether to revise or revert, never an automatic revert. |
| `inconclusive` | Everything else, including equal or worse raw rates on weak evidence. |

## Forwarding to pxx

`dx learn run --forward-pxx` writes `variant_preference` proposals into pxx's
`inbox/human-review-required/`, in pxx's exact `Proposal` shape (`model` /
`switch_model`, `basis: correlation`). `pxx improve triage` and `pxx promote`
then decide as they always have.

**Order of operations.** Forwarding runs after the pass's own write, inside
the same mutation lock every decision takes, so a reject cannot land between
the check and the write. For each recommendation that is still `PROPOSED`:

1. Commit a forward record in state `INTENT`. The same transaction includes
   the recommendation at the revision just read, so if anything decided it
   since, the commit fails and nothing reaches pxx.
2. Create the pxx entry under pxx's `cycle.lock`, opened with `"x"`: never
   overwriting, and never re-raising a signature a pxx human already decided.
3. Commit the outcome: `WRITTEN`, or `NOT_WRITTEN` with the reason.
   `NOT_WRITTEN` notes starting `retry:` are tried again on later passes.

**Interruption.** A crash between steps 1 and 3 leaves `INTENT`. Every
`dx learn run`, and every reject or retire, first reconciles each forward
record against what pxx's inbox actually holds:

- Our entry is there, or a pxx human has already decided it → `WRITTEN`.
- Nothing is there → `INTERRUPTED`, forwarded again if still `PROPOSED`.

The pxx filename is deterministic and created with `"x"`, so a retry cannot
produce a second entry. Entries are recognized as ours by a fingerprint over
the proposal fields that pxx's triage never rewrites.

**Rejecting, retiring, or going STALE in dx.** Inside the same lock, dx
withdraws its entry from pxx's inbox if that entry is still pending and still
ours. A foreign or already-decided entry is never touched. If `cycle.lock` is
busy, the withdrawal waits for the next pass. A STALE proposal is offered
again if a later pass sees its evidence return.

**Which pxx decisions count.** Only a human's: an entry in `qualified/` or
`rejected/` carrying pxx's `disposition` block, which is pxx's own rule. The
cycle's auto-rejections carry none, and pxx re-routes them itself, so they
neither block a forward nor hide dx's own pending entry. dx's pending entry
is always checked first.

**When pxx's cycle overwrites dx's entry.** pxx's cycle writes inbox files
without checking for an existing one. When that replaces dx's entry, the
forward record becomes `SUPERSEDED`. `queue` shows it, and dx offers the
proposal again once the slug clears.

**Blocked signatures.** While a signature is occupied, the blocked state is
recorded once. It is not re-committed every pass, and it does not bump the
recommendation's revision.

**pxx's triage takes no lock.** `pxx improve triage` reads the pending entry,
writes the decided copy, then unlinks the pending file, all without
`cycle.lock`. dx cannot serialize with it, so it does not pretend to:

- dx never overwrites, never removes anything but its own pending entry, and
  re-inspects after every change.
- When a dx human and a pxx human decide differently, the forward record
  gets a `divergence`. Examples: dx `REJECTED` while pxx `qualified` (possible
  even after a withdrawal, if triage had already read the entry), or dx
  `ACCEPTED` while pxx `rejected`. The divergence is audited and printed by
  `run`, `reject`/`retire` and `queue` until a human resolves it in one tool
  or the other.
- dx never resolves a divergence itself.
- The race can only be closed in pxx, by having triage take `cycle.lock`.

**pxx's coarse keys.** pxx keys inbox entries by `target:operation`. One pxx
human rejection of `model:switch_model` therefore blocks later model-switch
forwards, whichever model they name. dx learn keeps such proposals in its own
queue and records why.

## Storage and recovery

**JSON store (default, `DX_LEARN_STORE`).** All state lives in one directory
under an `fcntl` lock: `recommendations.json`, `adoptions.json`,
`forwards.json`, `audit.jsonl` and `passes.jsonl`. A transaction is written in
four steps:

1. A checksummed `journal.json`, durably: fsync, atomic rename, fsync of the
   directory.
2. The audit line, then the adoption, forward and recommendation files.
3. Each of those writes is durable in the same way.
4. The journal is removed last.

Every read and write first replays a pending journal. Replay is idempotent:

- The audit line is recognized by its hash. A torn half-line is trimmed only
  if it is a prefix of the journal's own line.
- A record already at its new revision is skipped.

So a crash at any write boundary yields the decision exactly once. A crash
before the journal is renamed into place yields no decision at all. The tests
kill the store at every boundary, including during recovery itself.

**Readers.** Readers take the same lock and replay first, including
`verify_audit` (and so `queue`), so no reader ever sees a half-written audit
line. `passes.jsonl` is an informational log, fsynced but not transactional:
nothing reads it back.

**Specs.** `accept` writes its spec inside the lock, after the decision
commits. If that write fails, the decision stands and
`dx learn spec <key> --out <path>` writes the spec from the stored proposal.

**When replay is refused.** If replay cannot be proven safe, the store raises
`RecoveryError` on *every* operation. Cases:

- The audit chain moved past the journal's anchor.
- A record is at an unexpected revision.
- A torn audit line is not the journal's.
- The journal's checksum does not match.

The error names the store, the journal, the transaction and the reason.
`dx learn recover` retries the replay or repeats the precise error. Resolving
a refused journal is a human's job: compare it with the named file, restore
from backup or finish the change by hand, then remove the journal.

**SQL Server (`DX_LEARN_MSSQL`) — UNQUALIFIED.** This backend has never run
against a live SQL Server. The build environment's network policy denied both
`packages.microsoft.com` (ODBC driver) and `mcr.microsoft.com` (server image).
Its logic has been exercised against a revision-aware fake:

- revision checks;
- `UPDATE … WHERE Revision = @read; SELECT @@ROWCOUNT` — read explicitly,
  because under `SET NOCOUNT ON` the driver's own rowcount is -1;
- rollback;
- the audit chain;
- `sp_getapplock` for the mutation lock.

The fake proves the logic, not the SQL dialect, driver behavior, locking
semantics or the schema DDL. dx learn refuses to use this backend unless
`DX_LEARN_MSSQL_UNQUALIFIED=accept` is set, and warns on every command when
it is.

To qualify it:

1. On a dev instance, apply `src/dx/learn_mssql.sql` (idempotent).
2. Run `pip install dx-orchestrator[mssql]`.
3. Run the store tests' scenarios against the live server (concurrent links,
   stale decisions, rollback, chain verification).

Once qualified, the backend also provides per-pass fact snapshots
(`dx_learn.Attempt`, `dx_learn.LedgerEvent`) and a `vYieldLatest` view for
T-SQL reporting.

## Instrumentation gaps found while building this

These limit what can be learned today. Each fix belongs upstream.

**pxx**
- Review findings are kept in memory only. `outcome.json` holds counts by severity, and the reviewer emits no category field. Persisting findings would let recurring review themes be learned.
- `pxx calibrate` results are printed, not persisted.

**psoperator**
- Audit rows carry no run, task or skill id.
- Risk-tier reasons are not written.
- `denied` conflates a human "no" with a timeout.
- Drills are indistinguishable from real stops.
- Harness `Evidence` and replay results are never persisted.

**dx**
- `role_task` evidence bundles are prunable, so dx learn does not depend on them.
- dx records no durations of its own, so cycle time (the scrum-master's first metric) is not learnable yet.

**sdlc-agent-roles**
- No role card owns cross-iteration learning. `scrum-master` is the nearest fit.
