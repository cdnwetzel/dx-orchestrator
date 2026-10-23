# Evidence-store hygiene

`dx run`, `dx merge`, and `dx verify-gui` write evidence bundles. With no
`--evidence-dir` and no `DX_EVIDENCE_DIR`, they land in the **default store**,
`~/.local/state/dx/evidence/<task>/<utc-timestamp>/` — deliberately outside any
repo under edit. Over a long dev session that store accumulates, and a store
nobody watches is confusing in a month. This is the rule for keeping it honest.

`dx doctor` reports what is sitting in the default store (families and counts),
so it is never an unwatched pile.

## What is safe to prune

The rule is a **type guarantee**, not a judgement call — it follows from what a
ledger row can reference:

| Family | A ledger row can reference it? | Prune |
| --- | --- | --- |
| `dx.role_task.v1` | No — nothing binds a role-task bundle | Freely |
| `dx.gui_verification.v1` | No | Freely |
| `dx.merge_gate.v1` | **Yes** — a green `dx merge` appends an `EVIDENCE` row binding the bundle's SHA-256 (the 0.18.0 / A1 receipt binding) | **Check the ledger first** |

So: **prune a bundle that attests to nothing; never prune one a ledger row
names.** Deleting a `merge_gate` bundle whose digest a row references leaves a
dangling reference in an append-only chain — which cannot be withdrawn (RL-009),
only corrected by appending. Role-task and gui-verification bundles have no such
hazard: nothing can point at them, so they are always safe to remove.

The append-only rule is the *ledger's* (RL-009). The evidence store does not
inherit it and should not — but it inherits the ledger's *references*.

## Checking before a merge-gate prune

For a `dx.merge_gate.v1` bundle, before deleting it, confirm no ledger row names
its digest:

```sh
# the bundle's digest is the sha256 of its SHA256SUMS (dx.merge_gate.v1 binding)
grep "dx.merge_gate.v1 sha256:$(sha256sum <bundle>/SHA256SUMS | cut -d' ' -f1)" <ledger>/ledger.jsonl
```

A hit means a row references it: do not delete it. No hit against any ledger you
kept means it is safe (a merge run against a throwaway ledger copy, as the test
suite does, references nothing that survives).

## Not the test suite's store

The test suite must never write into the real default store. `tests/conftest.py`
redirects `DX_EVIDENCE_DIR` to a throwaway dir for every test, **and** a
session-scoped guard fails the run if any bundle appears in the real store — the
class of leak that occurs when a subprocess is launched with a custom env that
drops `DX_EVIDENCE_DIR`. Any test that shells out to `dx` must set
`DX_EVIDENCE_DIR` in the subprocess env.

## Future: `dx evidence prune`

The enforcing version of this rule, if it earns its keep: `dx evidence prune
--dry-run` would list prunable bundles and **refuse** to delete any
`dx.merge_gate.v1` bundle a live ledger row still names. Until then, the rule
above and the `dx doctor` report are the working discipline.
