# GPG test fixtures

Public key material and detached signatures for three throwaway ed25519 keys.
**No private keys are here** — these files only let a verifier check signatures
that were made once, at generation time.

| Key | State | What gpg reports for its signature |
| --- | --- | --- |
| `valid` — Bob Reviewer | usable | `GOODSIG` + `VALIDSIG`, exit 0 |
| `revoked` — Rev Signer | revoked | `REVKEYSIG` + `KEYREVOKED` + `VALIDSIG`, **exit 0** |
| `expired` — Exp Signer | expired | `EXPKEYSIG` + `KEYEXPIRED` + `VALIDSIG`, **exit 0** |

Every signature is over `payload.bin`, which is exactly the canonical RL-003
approval message `T-TEST` + `"a"*64` + `code_review` (81 bytes, no trailing
newline — the same shape as the real `approvals/*.msg` files).

The two `exit 0` rows are the point. A revoked or expired key still produces a
cryptographically valid signature, and `gpg --verify` reports success for it.
Gating on the return code therefore lets a retired approval key through the
RL-003 check. `dx` requires `GOODSIG` and rejects the degraded codes by name.

## Why these are committed rather than generated per-run

The first version of these tests generated the keys at runtime and waited out a
one-second expiry. It failed roughly one run in ten. The cause was not `dx`:
under WSL2 the system clock was observed jumping **backwards** by ~4 seconds
mid-test, so a key created at `T` with expiry `T+1s` could read as un-expired
afterwards. Any wall-clock-expiry test is unstable on such a host.

Committed fixtures remove time from the test entirely — these keys expired at a
fixed instant in the past and will read as expired on any future clock.

## Regenerating

```bash
python tests/fixtures/gpg/regenerate.py
```

Run from the repository root. It generates fresh keys in a temporary GNUPGHOME,
revokes one, waits out the expiry of another, writes the seven files here, and
re-verifies each in a pristine keyring before exiting. Expect it to take a few
seconds — it has to wait for a real expiry once, so you don't have to.
