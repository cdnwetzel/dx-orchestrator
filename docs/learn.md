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
| pxx runs | `<state_dir>/runs/<id>/{outcome,task,manifest}.json`, `events.jsonl` | No chain exists. A malformed run is quarantined and named in the report. A run without `outcome.json` is counted and skipped. |
| devswarm ledger | `DX_LEDGER_REPO` | `get_ledger_head()` (the ledger's own `verify_chain.py`) must pass before any row is read; a broken chain stops the whole pass and leaves the queue unchanged. The reference ledger is refused without `--allow-reference-ledger`. A task with a `CORRECTION` row is set aside rather than guessed at. |
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
a human. `dx`, `claude`, `agent` and blank are refused.

**Audit.** Every decision is appended to a hash-chained `audit.jsonl` (JSON
store) or to an append-only, trigger-guarded table (MSSQL) before the state
changes.

**Ordering.** `queue` prints earlier adoptions before new proposals. This
follows the scrum-master card: the last set of retro actions is checked before
new ones are added.

## Forwarding to pxx

`dx learn run --forward-pxx` writes `variant_preference` proposals into pxx's
`inbox/human-review-required/`:

- The entry is in pxx's exact `Proposal` shape (`model` / `switch_model`, `basis: correlation`).
- The write happens under pxx's own `cycle.lock`.
- The file is opened with `"x"`, so it never overwrites an existing entry.
- A signature that a human already dispositioned in pxx is never re-raised.

`pxx improve triage` and `pxx promote` then decide as they always have.

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
