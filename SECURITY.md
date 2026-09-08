# Security policy

## Reporting a vulnerability

Report privately through GitHub Security Advisories:
**https://github.com/cdnwetzel/dx-orchestrator/security/advisories/new**

Please do not open a public issue for a vulnerability first. Include the version
(`dx --version`), what an attacker gains, and the smallest reproduction you have.
Expect an acknowledgement within a week.

## What dx is, in security terms

`dx` is a **gate**, not a sandbox. It decides whether a change may proceed, and
its security value is entirely in refusing to say yes when it should not. The
threat it is built against is a capable, fast, non-malicious agent producing
changes faster than a human can verify them — and the failure it is built to
prevent is a change merging without accountable human review.

That shapes what counts as a vulnerability here. **Anything that lets a gate
return "pass" when the invariant does not hold is a security bug**, even with no
attacker in the picture. Three such bugs shipped in 0.2.0 and were fixed in
0.3.0; see the `CHANGELOG.md` entry for that version.

## Trust boundaries

| Boundary | Position |
| --- | --- |
| Approval keys | **Never harness-reachable** (RL-010). `dx` verifies signatures; it never creates them, and it never holds a passphrase. Signing happens on a trusted terminal, outside any agent context. |
| The keyring | `dx merge` imports registered public keys into a scratch `--homedir` per invocation. It never reads or writes the user's `~/.gnupg`. |
| The ledger | `cdnwetzel/devswarm-ledger` is authoritative. `dx` never computes chain state itself; it shells out to that repo's own `tools/verify_chain.py` and surfaces the result unchanged. |
| Local models | **Never a gate** (RL-007). The GUI verifier's answer is advisory evidence recorded alongside a decision; it cannot make one. Every gate is regex, YAML, or GPG. |
| Inference endpoints | Treated as untrusted output sources. Model output is written to files under `pxx`'s control and reviewed like any other diff. |
| The network | `dx` never listens on a port and never sends telemetry. All connections are outbound to endpoints you configure. |

## What dx does not protect against

Stated plainly, because a gate that overstates its coverage is worse than none:

- **A compromised signing key.** If an approver's private key is stolen, `dx`
  will honour signatures made with it until the key is revoked and the
  revocation reaches `docs/keys/`.
- **A malicious ledger.** `dx` trusts the ledger clone it is pointed at. Point
  it at a hostile one via `DX_LEDGER_REPO` and it will verify against that.
- **Malicious role cards.** Card text is injected into the model prompt. Cards
  come from a repo you control; treat write access to it as equivalent to
  prompt-injection access.
- **What the model writes.** `dx` routes generation and gates the merge. It does
  not analyse generated code for vulnerabilities — that is the reviewer's job,
  and the reason a human signature is required at all.
- **The GUI verifier's judgement.** A vision model can be wrong or fooled. It is
  never the sole basis for a merge (RL-007).
- **Anything after `--force`.** `--force` bypasses every gate by design, for
  emergency rollback. It announces itself on stderr; it does not stop you.

## Currently stubbed

`dx merge` verifies but **does not yet perform the merge or append to the
ledger**. Do not read a successful `dx merge` as "this was merged and recorded" —
it means "this passed the signature gate." The `TODO(ledger)` in `cmd_merge.py`
marks the boundary. `dx run` does write evidence bundles as of 0.9.0; they are
tamper-evident (`sha256sum -c SHA256SUMS`) but unsigned — they prove a file was
not altered since the bundle was written, not who wrote it.

## Verifying a release

```bash
git verify-tag v0.4.0          # if the tag is signed
pip install -e ".[dev]"
pytest                          # includes real-GPG integration tests
```

The suite in `tests/test_gpg_integration.py` runs against committed keys —
valid, revoked, and expired — and asserts that a revoked or expired key is
refused. Those tests exist specifically so the signature gate cannot silently
regress to accepting one.
