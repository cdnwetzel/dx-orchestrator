# Artifacts

Committed evidence from real runs, kept in the repo's history the way a verified
ledger row is — so the claims the code makes can be checked, not just read.

## `first-live-staged-ledger.jsonl`

**The first live `EVIDENCE → REVIEWED → SIGNED → EXECUTED` ledger produced by the
staged-action flow.** Produced by **dx 0.19.0** on 2026-09-11, on the AT-SPI host
(Xvfb `:99`, GTK invoice fixture), by `examples/staged_harness_live.py` — the
three-act harness bound to real I/O: a real `dx.staged_action.v1` bundle, a real
detached GPG signature, a real hash-chained ledger, and a real Xvfb window move.

Twelve rows, one genesis and three acts:

| act | rows | outcome |
|-----|------|---------|
| genesis | GENESIS | — |
| 1 — full loop | EVIDENCE, REVIEWED, SIGNED, EXECUTED | executed |
| 2 — rejection | EVIDENCE, REVIEWED, ABANDONED | rejected (re-proposal gate refused the unchanged stage) |
| 3 — stale refusal | EVIDENCE, REVIEWED, SIGNED, INCOMPLETE | refused — the frame moved between signing and execution, the runner's re-verification caught it, and the executor was never called |

The `SIGNED` rows record `mechanism = software-ceremony (RL-010-non-compliant)`:
the demo signs with a throwaway software key, honestly marked as the transitional
fallback until a hardware token lands (RL-010, Decision 0017). The point this run
proves is the governance chain, not the key grade — swap the key for a
non-exportable token and the same rows read `openpgp-card`.

### Verify it yourself

The chain is checked by the same tool dx treats as the contract:

```sh
python3 <devswarm-ledger-reference>/tools/verify_chain.py \
  docs/artifacts/first-live-staged-ledger.jsonl
# OK: 12 row(s) verified. Ledger head hash: 0c461c73fbe145156c308ec03560b4a9e8e942ef9b6f29892e5f67478b364df7
```

Every row's `prev_hash` is the SHA-256 of the canonical form of the row before
it, so any edit to any row breaks the chain from that point on. An external
examiner needs only this file and the verifier — no trust in the machine that
produced it.
