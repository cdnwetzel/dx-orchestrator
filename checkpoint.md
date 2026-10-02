# checkpoint.md

**Last updated:** 2026-10-02
**Version:** 0.21.2

## Where we are

`dx-orchestrator` is public, MIT, and is the merge gate and runner behind a governed
coding pipeline that has merged real work through every step (chat → bridge → `dx run`
→ `pxx loop --sandbox` → psguard → review → detached signature → `dx merge`). Release
detail is in `CHANGELOG.md`; this file is for resuming, not for history.

**Verified (2026-10-02, 0.21.2):**

| Check | Result |
| --- | --- |
| `pytest` | 967 collected; CI green on 3.11, 3.12, 3.13 |
| `ruff check .` | clean |
| `mypy src/dx --strict` | clean |
| `python -m build` | passes |
| `dx --version` | `dx 0.21.2` |
| `dx merge` | RL-003 checks, then the `approval_tier` check over the chain diff (0.20.0), `mechanism=` from the ledger's key registry, SIGNED/MERGED rows with `approval_tier=`/`mechanism=`/`sod_exception=` |
| `dx tier` | the gate's tier decision, read-only, `--json` |
| `dx run` | records EXECUTED with provenance (`run= model= pxx=`) only when HEAD moved; redlines a control-plane contact at EXECUTED (0.21.0) |
| ledger state | a REVIEWED row after MERGED is a counted sample, not a reopening (0.21.2) |

## Since 0.10.0 (the previous checkpoint), in one paragraph

0.11–0.19: evidence bundles for every command, `dx.ledger_state` as the one reading of a
task's rows, `dx run` executing `pxx loop --sandbox` and reading the loop's own test
record, EXECUTED recorded by the runner, salvage of work a refusal discarded, rework
seeding from a predecessor's recorded diff, model-residency probes in `dx doctor`.
0.20.0: charter Decision 0020 in the gate (approval tiers, key registry, mechanism).
0.21.x: REDLINE at EXECUTED for control-plane contact; gate-written rows name no human;
samples on merged work. Docs: `docs/approval-tiers.md`, `docs/evidence-store-hygiene.md`.

## Next real work

Tracked in the PS Coding implementation plan (AskPS, signed 2026-10-02): WP-4 (the
reviewer leg: explicit `--review`/`--review-mode advisory` from the manifest, review facts
in the bundle, doctor posture), WP-3 (task branches so sign-off is a real gate; `dx merge`
performs a two-parent merge; `AlreadyOnBranch` becomes an anomaly), then `seed.py`
staging deletions from a predecessor's patch, NUL-delimited git output elsewhere.

## Things NOT to do

- Do not merge against the reference ledger without `--allow-reference-ledger`: its rows
  attest to nothing.
- Do not take `mechanism=` or the tier from a flag; both are derived.
- Do not edit a file that has a detached signature beside it; write a companion.
- Lab topology is not recorded in this repo; the live routing table is the manifest on each
  driver box.

## Sibling repos

`pxx` (2.6.1 on PyPI; execution hosts may pin lower), `psoperator` (GUI verification),
`devswarm-ledger-reference` (public fixture ledger, with its own README drift check),
`sdlc-agent-roles` (the role cards).
