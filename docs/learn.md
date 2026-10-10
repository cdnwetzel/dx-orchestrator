# dx learn — continuous improvement from past iterations

`dx learn` reads what already happened across the stack, ranks what is worth
changing, and stops. A named human accepts or rejects each proposal. An
accepted proposal becomes a dx task spec that goes through `dx run` and a
signed `dx merge` like any other change. Once that task merges, `dx learn
efficacy` judges it against the metric fixed when it was proposed. Changes
that did not work come back as proposals to revise or revert.

```
 pxx runs ─┐                     ┌─ PROPOSED ──accept (human)──► dx task spec ─► dx run ─► dx merge (signed)
 ledger ───┤  verify ─► learn ─► │      │                                                      │
 psoperator┤  (fail     (pure,   │      └─reject (human)─► stays down until evidence ≥ 2×      │ MERGED row
 reviews ──┘   closed)  seeded)  └─ model routing ─► pxx inbox/human-review-required          ▼
                                                     (pxx triage + promote decide)     efficacy: did the
                                                                                       pre-registered rate fall?
                                                                                       no ─► adoption_ineffective
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
| `adoption_ineffective` | An adoption's rate did not fall after its task merged: P(fell) < 0.5 with ≥ 10 observations after. | the adoption's metric |

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

**Concurrency.** Every write is compare-and-set against the status the writer
read.
- A pass never overwrites a decision made while it ran; it reports the
  record as left alone.
- A second decision made from a stale read is refused with nothing recorded.

**Audit.** Every decision is appended to the audit chain before the state
changes.
- The JSON store keeps a hash-chained `audit.jsonl`.
- MSSQL uses an append-only, trigger-guarded table carrying the same chain
  (`EntryJson`, `PrevHash`, `Hash`).
- The chain is verified before every decision, and a chain that does not
  verify blocks further decisions.
- `queue` prints the chain's state first.

**Ordering.** `queue` prints earlier adoptions before new proposals. This
follows the scrum-master card: the last set of retro actions is checked before
new ones are added.

## Efficacy statuses

| Status | Meaning |
| --- | --- |
| `awaiting-task` / `awaiting-merge` | No implementing task linked yet, or the task has no MERGED row at or after acceptance. |
| `merged-before-adoption` | The linked task merged before the proposal was accepted; it cannot be the change. |
| `source-not-read` | The metric's source was not read this pass. |
| `measuring (before a/10, after b/10)` | Fewer than 10 observations on either side of the merge (60-day window before). |
| `no-baseline-problem` | Nothing was failing before the merge, so there is nothing to credit. |
| `effective` | P(rate fell) ≥ 0.90 and the raw rate fell. |
| `ineffective` | The raw rate did not fall. Raised as `adoption_ineffective`. |
| `inconclusive` | Anything else. |

## Forwarding to pxx

`dx learn run --forward-pxx` writes `variant_preference` proposals into pxx's
`inbox/human-review-required/`:

- The entry is in pxx's exact `Proposal` shape (`model` / `switch_model`, `basis: correlation`).
- The write happens under pxx's own `cycle.lock`.
- The file is opened with `"x"`, so it never overwrites an existing entry.
- A signature that a human already dispositioned in pxx is never re-raised.

`pxx improve triage` and `pxx promote` then decide as they always have.

Three gaps in pxx's own inbox handling, and how dx learn handles each:

- **Triage does not lock.** `pxx improve triage` does not take `cycle.lock`, so
  a human can disposition the signature while dx is writing. dx re-checks
  after writing and withdraws its entry if that happened. A very narrow race
  remains, which only pxx can close by taking the lock in triage.
- **Overwrites.** pxx's own cycle may overwrite a forwarded entry with the
  same signature. That is the same proposed change, worded by pxx.
- **Rejecting in dx.** Rejecting in dx learn does not touch pxx's inbox; it
  prints the `pxx improve triage reject <slug>` to run.

Known coarseness: pxx keys inbox entries by `target:operation`. One human
rejection of `model:switch_model` in pxx therefore blocks every later
model-switch forward, whichever model it names. dx learn keeps such proposals
in its own queue and says why it did not forward them.

## Storage

The default store is a JSON directory (`DX_LEARN_STORE`): atomic writes,
`fcntl` locking, and a refusal to overwrite a file it cannot parse.

To use SQL Server instead:

1. Set `DX_LEARN_MSSQL` to an ODBC connection string.
2. Run `pip install dx-orchestrator[mssql]`.
3. Apply `src/dx/learn_mssql.sql` (idempotent).

The SQL Server backend adds per-pass fact snapshots (`dx_learn.Attempt`,
`dx_learn.LedgerEvent`) and a `vYieldLatest` view for T-SQL reporting. The
MSSQL code path is covered by shape tests only (parameter counts, transaction
order, rollback). Run it against a real instance before relying on it.

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
