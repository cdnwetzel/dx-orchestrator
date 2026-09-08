# dx-orchestrator Tutorial: Build a Sovereign AI Factory

*~30 minutes from a clean box to a real code-generation task on your own hardware.*

Everything below was captured live from a Surface Pro 6 running Ubuntu 24.04 in WSL2, dispatching to a vLLM endpoint on the LAN. Any command output shown is real. Any place where dx is not yet doing what you might expect, this document says "not yet" instead of pretending.

> **One declared edit.** Host addresses and usernames in these transcripts have been
> replaced with stable placeholder names — `t5810.lab`, `asrock.lab`, `orin.lab`,
> `macmini.lab`, `operator@…` — so this document does not publish a private network
> topology. The substitution is consistent throughout. Nothing else in any transcript
> has been altered: no output was reworded, reordered, or invented.

---

## 0. What you're building

```
you → dx CLI → role card (governance) → hardware manifest (routing) → pxx → your GPU → files
                                                                              ↓
                                                              (dx merge gate ← devswarm-ledger)
```

`dx` is a thin control plane. It does three things on top of raw `pxx`:

- **Governance** — every task must be run under a named role card (one of 38, shipped in `sdlc-agent-roles`). The card's *Mandate* and *Must not* sections are injected into the agent prompt.
- **Hardware routing** — a manifest maps each role to an inference endpoint and model, so heavy roles hit the T5810 vLLM and light ones hit an Ollama box.
- **Merge gate** — `dx merge` verifies a GPG-signed approval against `devswarm-ledger/SCHEMA.md` before allowing a merge. Currently enforces the RL-003 signature contract; **does not yet** perform the actual git merge or ledger append (deliberately stubbed until Gate 1 unpauses).

What dx does **not** do today, honestly:
- No ledger write. `dx run` writes evidence bundles; `dx merge` verifies an approval and stops.
- No actual git-merge or ledger-append (the merge gate verifies but doesn't commit).
- No GUI end-to-end run has been exercised (`dx verify-gui` code path works but hasn't been demonstrated on this box).

Every mechanical gate described below has a regression test. The suite is
hermetic — no sibling clones, no keyring, no network — so you can run it before
you have configured anything. That matters here: writing
those tests surfaced three gates that were failing *open*, all fixed in 0.3.0.

---

## 1. Prerequisites

| Requirement | Why | Check |
|---|---|---|
| Python 3.11+ | dx + pxx floor | `python3 --version` |
| `virtualenv` or `python3-venv` | Ubuntu 24.04 refuses bare `pip install` (PEP 668) | `virtualenv --version` |
| `git` | clones + pxx safety-net tags | `git --version` |
| `gpg` | verifies the merge gate | `gpg --version` |
| A reachable inference endpoint | e.g. T5810 vLLM `t5810.lab:8007` | `curl -s http://t5810.lab:8007/v1/models \| head -c 200` |


---

## 2. Install

**A venv is mandatory** on Ubuntu 24.04+. `pip install -e .` outside a venv fails with `error: externally-managed-environment`.

```bash
cd ~/ai
git clone https://github.com/cdnwetzel/dx-orchestrator.git
cd dx-orchestrator

# Use virtualenv if python3-venv isn't installed (avoids an apt round-trip)
virtualenv -p python3 .venv || python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"      # drop [dev] if you don't want pytest + ruff
./scripts/setup_dependencies.sh
```

The setup script is idempotent and preflight-checks that a venv is active and `gh` is authenticated. It will:

- Install `pxx-orchestrator>=2.5.4` from PyPI
- Clone `~/ai/psoperator` and `pip install -e .` it
- Clone `~/ai/sdlc-agent-roles`
- Clone `~/ai/devswarm-ledger-reference`
- Seed `~/.config/dx/hardware_manifest.yml` with a **placeholder** routing table

If a step fails, it exits non-zero with a specific error and you can re-run after fixing.

Verify the install before configuring anything:

```bash
dx --version    # dx 0.9.0
pytest          # all green
```

---

## 3. Configure the hardware manifest

The seeded manifest contains placeholder hosts and `REPLACE-WITH-YOUR-MODEL` models — `dx doctor` will flag every one as unreachable until you edit it. Here is a filled-in example in the shape that actually works:

```yaml
# Endpoints MUST NOT include a trailing /v1.
# pxx appends the right suffix per `provider`:
#   ollama → {endpoint}/api/tags
#   vllm   → {endpoint}/v1/models
#   openai → {endpoint}/v1/models
# If you leave /v1 in the endpoint, probes hit /v1/v1/models → 404.

roles:
  backend-engineer:
    endpoint: "http://t5810.lab:8007"
    provider: "vllm"
    model: "qwen3.8-27b"

  frontend-engineer:
    endpoint: "http://asrock.lab:11434"
    provider: "ollama"
    model: "q36-moe:latest"

  security-architect:
    endpoint: "http://t5810.lab:8007"     # DGX substitute — dgx.lab offline
    provider: "vllm"
    model: "qwen3.8-27b"

  default:
    endpoint: "http://localhost:11434"
    provider: "ollama"

gui_verification:
  vlm_endpoint: "http://orin.lab:11434/api/generate"
  vlm_model: "qwen2.5vl:3b"
  ssh_host: "operator@orin.lab"
  screenshot_cmd: "import -window root -"
```

If your role cards live somewhere other than `~/ai/sdlc-agent-roles/skills/sdlc-role/roles`, either add a top-level `roles_path:` key to this file or set `DX_ROLES_PATH`.

**Why the `/v1` warning is non-negotiable.** In an earlier iteration the manifest had `endpoint: "http://t5810.lab:8007/v1"` and every `dx run` failed with `[MODEL_UNAVAILABLE] http://t5810.lab:8007/v1 returned HTTP 404`. Root cause: `pxx/router.py` constructs probe URLs as `{base}/v1/models`, so the `/v1` doubles. Fix is to strip it. Documented in commit `c5cd52d` and captured here so nobody else has to rediscover it.

---

## 4. Sanity check: `dx doctor`

```bash
dx doctor
```

Actual observed output on this box:

```
🔍 dx doctor — self-test

✅ Python 3.12 (>= 3.11)
✅ pxx installed (/home/cwe/ai/dx-orchestrator/.venv/bin/pxx)
✅ PSOperator importable
✅ PSOperator run_agent script at /home/cwe/ai/psoperator/examples/run_agent.py
✅ Role cards parse cleanly (38 files at /home/cwe/ai/sdlc-agent-roles/skills/sdlc-role/roles)
✅ devswarm-ledger at /home/cwe/ai/devswarm-ledger-reference
✅ gpg installed (/usr/bin/gpg)
✅ Hardware manifest at /home/cwe/.config/dx/hardware_manifest.yml
   (parses, and every section has the expected shape)

🌐 Network checks (non-critical):
✅ role:backend-engineer → t5810.lab:8007 reachable
✅ role:frontend-engineer → asrock.lab:11434 reachable
✅ role:default → macmini.lab:11434 reachable
✅ gui.vlm_endpoint → orin.lab:11434 reachable

✅ All core checks passed. dx is ready to use.
```

The network probes are TCP-connect only (`socket.create_connection`), so a live vLLM that 404s on `/` still shows reachable. Add `--no-network` to skip the probes.

`dx doctor` is deliberately more than a liveness check. It parses every role card (a card that fails to parse is dropped from the registry, which for an Anchored role would silently remove its hard-block), loads the manifest exactly the way `dx run` does rather than only checking that the YAML is well-formed, and warns about a trailing `/v1` on any endpoint. A green doctor is meant to mean *this install works*, not *these files exist*.

---

## 5. Look at the constitution: `dx roles list`

```bash
dx roles list --fit High
```

Actual output — all 11 High-fit cards (column widths are sized from the data,
so a filtered list is narrower than an unfiltered one):

```
Slug                  Fit   Seat                      Anchored
----------------------------------------------------------------
backend-engineer      High  S4                        
code-reviewer         High  All engineers (rotating)  
data-engineer         High  S5                        
finops                High  S2 + S7                   
frontend-engineer     High  S6                        
mobile-engineer       High  S6                        
performance-engineer  High  S4 + S8                   
platform-engineer     High  S7                        
product-analyst       High  S1                        
sdet                  High  S8                        
technical-writer      High  S6 / S7 / S9 split        
```

Fit distribution across all 38 cards:
- **High** (11): fully delegable to an agent
- **Partial** (20): agent drafts, human decides
- **Anchored** (7): requires a named accountable human; `dx run` refuses to execute autonomously

Inspect one card:

```bash
dx roles list --slug security-architect
```

Prints the mandate and the "Must not" section — the second is what dx enforces mechanically (it becomes part of every prompt when that role is loaded).

Validate structural integrity:

```bash
dx roles validate
```

Runs six mechanical checks per card (fit/anchored consistency, seat present, non-empty mandate + must_not, anchored roles have handoff, slug format). Expected: `✅ PASS: 38 role cards validated.`

---

## 6. Live code generation: `dx run`

**Prerequisite**: pxx has a fail-closed shell-verify gate that fires after every successful edit. In a headless WSL2 environment without a preconfigured hook, that gate makes pxx exit non-zero *even when the file was written correctly*. Two ways past it:

- **Permissive** (tutorial-friendly, matches how pxx's own R-001 receipt was recorded): `export PXX_ALLOW_UNGATED_SHELL=1`
- **Correct** (production): configure a PreToolUse hook per `~/ai/pxx/docs/CONFIG.md §hooks`

For this tutorial, use the permissive setting:

```bash
export PXX_ALLOW_UNGATED_SHELL=1
```

Now prep a throwaway workspace. pxx needs a git repo (it creates `pxx-pre/` safety tags):

```bash
mkdir -p /tmp/dx-live-test && cd /tmp/dx-live-test
git init -q
git commit --allow-empty -q -m "seed"
```

Dry-run first (no inference, no writes — proves routing):

```bash
dx run T-LIVE-001 --required_role backend-engineer --scope . \
    --no-commit --message "trivial" --dry-run
```

Actual output:

```
DRY RUN
  task_id:       T-LIVE-001
  required_role: backend-engineer
  fit:           High
  PXX_BASE_URL:  http://t5810.lab:8007
  PXX_MODEL:     qwen3.8-27b
  PXX_PROVIDER:  vllm
  pxx:           /home/cwe/ai/dx-orchestrator/.venv/bin/pxx
  command:       /home/cwe/ai/dx-orchestrator/.venv/bin/pxx edit --scope . [--commit] <prompt>
```

Now the real run against T5810:

```bash
dx run T-LIVE-001 --required_role backend-engineer --scope . --no-commit \
    --message "Write a Python file called hello.py with a function greet(name) that returns 'Hello, ' + name. Include a main() that prints greet('World') when run directly."
```

Actual output, with `PXX_ALLOW_UNGATED_SHELL=1` exported as instructed above:

```
🚀 Running task T-LIVE-001 with role backend-engineer on http://t5810.lab:8007 (model: qwen3.8-27b)...
[COMPLETED] Created `/tmp/dx-live-test/hello.py` with:

- `greet(name)` — returns `'Hello, ' + name`
- `main()` — prints `greet('World')`, invoked via the `if __name__ == '__main__':` guard

Verified by running it: outputs `Hello, World` (exit 0). [net: pxx-pre/20260908T043102Z] (rounds=3 tokens=6243 diff_lines=10)
✅ Task T-LIVE-001 completed.
```

Exit code: `0`.

**If you skipped the export**, you get this instead — worth recognising, because the
task actually succeeded:

```
🚀 Running task T-LIVE-001 with role backend-engineer on http://t5810.lab:8007 (model: qwen3.8-27b)...
[HOOKS_MISSING] run_shell in permission mode 'edit' requires a shell safeguard (fail-closed); none is configured. Choose one: [...] — 1 file already modified: hello.py [net: pxx-pre/...] (rounds=0 tokens=0 diff_lines=11)
❌ pxx task failed (pxx exit 2).
```

Exit code `3` — `dx`'s code for "the task ran and failed", distinct from `2`,
which means governance refused the role (§8). pxx's own exit code is reported in
the message rather than returned, so a caller can always tell the two apart.
**And `hello.py` was written** — pxx says so in its own error line:
`1 file already modified: hello.py [...] diff_lines=11`. That is the fail-closed
shell-verify gate firing *after* the edit landed, not instead of it.

Verify the file:

```bash
cat hello.py
```

Actual generated output (Qwen3.8-27B-FP8):

```python
def greet(name):
    return 'Hello, ' + name


def main():
    print(greet('World'))


if __name__ == '__main__':
    main()
```

Yours will not match byte for byte. This is a language model, not a template
engine — the same prompt on the same endpoint produced a version with a
docstring on an earlier run and this one without. What should be stable is the
*shape*: a `greet` that concatenates, a `main` behind an `if __name__` guard,
and a file that runs.

Run it:

```bash
python3 hello.py
# Hello, World
```

Clean up:

```bash
cd / && rm -rf /tmp/dx-live-test
```

**Anatomy of what just happened:**

1. `dx run` loaded `backend-engineer.md` from the role registry.
2. It built a prompt: `[ROLE: backend-engineer (Fit: High)] MANDATE: ... MUST NOT: ... USER INSTRUCTION: ...`
3. It looked up `backend-engineer` in the manifest, set `PXX_BASE_URL=http://t5810.lab:8007`, `PXX_MODEL=qwen3.8-27b`, `PXX_PROVIDER=vllm`.
4. It ran `pxx edit --scope . --message '<prompt>'` under those env vars.
5. pxx tagged `pxx-pre/<timestamp>` (its safety net), edited `hello.py`, then hit its shell-verify gate and exited non-zero.
6. The edit survived — pxx's atomic write happens before the verify step.

---

## 7. The merge gate: `dx merge`

`dx merge` currently enforces three RL-003 signature checks (per `devswarm-ledger/SCHEMA.md § approvals/`). It does **not** yet perform the actual git merge or append `SIGNED`/`MERGED` rows to the ledger — those are deliberately stubbed until DevSwarmX Gate 1 unpauses.

The three checks:

1. **Chain verify** — shells out to `devswarm-ledger/tools/verify_chain.py` to get the current head hash.
2. **Signature verify** — imports every key from `devswarm-ledger/docs/keys/*.asc` into a scratch keyring and runs `gpg --verify <sig> <msg>`.
3. **Payload + separation** — checks that the signed message equals `task_id + current_head + role` exactly, and that the signer's name differs from the task's `author_human` recorded in the ledger.

`setup_dependencies.sh` clones `devswarm-ledger-reference`, a public ledger whose
three rows are synthetic and whose one approval is a real GPG signature. It exists
so you can run every branch of this gate without an operational ledger of your own.

```bash
dx merge T-0001
```

Actual output on this box:

```
✅ Ledger chain verifies. Head: b8b8baca959ddfbe…
✅ Signature verified. Signer: Rex Reviewer <reviewer@example.invalid>
✅ Signed message binds task_id + current head + role.
✅ Separation of duties: author 'Ada Author' ≠ signer 'Rex Reviewer'.
✅ All RL-003 checks passed for T-0001.
🔄 Merging T-0001... (stub — git merge + ledger append not wired yet)
```

Exit code: `0`. Four checks, each of which can fail on its own.

**Now make it fail.** The signature binds to a specific ledger head, so appending
any row invalidates it — that is the mechanism, not a bug. Append a row to
`~/ai/devswarm-ledger-reference/ledger.jsonl` (the repo's README shows how) and
re-run:

```
✅ Ledger chain verifies. Head: ed127b72ca5642a3…
✅ Signature verified. Signer: Rex Reviewer <reviewer@example.invalid>
❌ Stale signature (RL-003). Signed head b8b8baca959ddfbe… but current head is ed127b72ca5642a3…. Re-sign after re-verifying the chain.
```

Exit code: `1`. Note check 2 still passes — the signature is cryptographically
valid. It is *stale*, and staleness is the whole reason an approval binds to a
head hash: you approve this exact state of the world, not "this task, whenever".
`tools/new_demo_approval.sh` in the reference ledger re-signs against the new head.

Note the verdicts read in the order they were decided, even piped into `cat`. Through 0.2.0 they did not: passes went to stdout (block-buffered when not a tty) and failures to stderr (unbuffered), so a redirected transcript listed the failure *before* the checks that preceded it. A gate transcript is evidence, so every verdict is now flushed as it is reached, with a subprocess test pinning it.

Bypass with `--force` (audit-visible on stderr):

```bash
dx merge T-0001 --force
```

Output:

```
⚠️  --force in effect for T-0001: bypassing the RL-003 signature check.
🔄 Merging T-0001... (stub — wire to devswarm-ledger)
```

Exit code: `0`. Pass `--verify-gui` as well and the banner says the GUI check was skipped too — `--force` bypasses every gate, and the message now names all of them.

**What the gate rejects.** Beyond a stale head: a payload bound to a different task, a trailing newline in the `.msg` file, a signer whose name matches the ledger's `author_human`, and — as of 0.3.0 — a signature made by a **revoked or expired key**. That last one used to pass. `gpg --verify` exits 0 and emits `VALIDSIG` for a signature from a revoked key, so checking the return code was not sufficient; verification now requires `GOODSIG` and rejects `REVKEYSIG` / `EXPKEYSIG` / `KEYREVOKED` / `KEYEXPIRED` / `EXPSIG` / `SIGEXPIRED` by name.

**What is still not demonstrated**: this gate running against a *live operational*
ledger with a freshly-made RL-010 signature. The green run above is real — a real
`gpg --verify` against a real key — but the ledger is a published reference whose
rows attest to no work, and its demo key was generated by a script, which is
precisely what RL-010 forbids for a real approval. A production signature must be
produced interactively on a trusted terminal with a passphrase no agent ever
holds. The gate is proven; the ceremony around it is not.

---

## 8. Try a role you're not authorized for

Anchored roles are hard-blocked in `dx run`:

```bash
dx run T-BLOCK --required_role domain-sme --message "confirm a fact" --dry-run
```

Output:

```
🔒 Role 'domain-sme' is Anchored (seat: Borrowed).
   A named accountable human must issue this decision. dx will not run it autonomously.

   Handoff instructions from the card:
   **Receives from:** `product-manager`, `business-analyst`, `qa-analyst`
   **Hands to:** `business-analyst` (corrected requirements), `uat-coordinator` (acceptance realism)

   To proceed anyway (audit-visible), re-run with --force.
```

Exit: `2`. This is deliberate — the seven anchored roles (`domain-sme`, `engineering-manager`, `uat-coordinator`, `incident-commander`, `customer-success`, `compliance-privacy`, `legal-contracts`) refuse autonomous execution.

`--force` steps past it, and says so on stderr:

```
⚠️  --force in effect: running Anchored role 'domain-sme' (seat: Borrowed) autonomously, past a separation-of-duties invariant.
DRY RUN
  task_id:       T-BLOCK
  ...
```

Through 0.2.0 the flag was documented as "audit-visible" and printed nothing at all. A bypass that leaves no record is not audit-visible.

---

## 9. What's stubbed (be honest with yourself)

Working today, each with regression tests:
- ✅ Role parsing + validation + injection into the pxx prompt
- ✅ Hardware routing (endpoint + model + provider per role)
- ✅ Anchored hard-block in `dx run`, with an audit-visible `--force`
- ✅ Three-part RL-003 signature verification in `dx merge`, rejecting revoked and expired keys
- ✅ GUI verification code path (`dx verify-gui`) — the code is real, the SSH screenshot + VLM call work

Fixed in 0.3.0 because a test caught it, not because anyone noticed in use:
- 🐛 Separation of duties failed **open** whenever the signer's GPG uid had no
  `(comment)` field — the name kept its `<email>`, so it could never equal the
  ledger's `author_human`. An author could have approved their own task.
- 🐛 Revoked and expired signing keys cleared the RL-003 gate.
- 🐛 `dx verify-gui` had a real host and SSH username baked in as fallbacks, so an
  unconfigured install would silently SSH to somebody else's machine.

Deliberately deferred until DevSwarmX Gate 1 unpauses:
- ✅ `dx run` writing `dx.role_task.v1` evidence bundles — shipped in 0.9.0, verified with `sha256sum -c`
- ⏸ `dx merge` doing the actual `git merge --no-ff` and appending `SIGNED`/`MERGED` rows to `devswarm-ledger/ledger.jsonl`
- ⏸ pxx review-independence config (`PXX_REVIEW_MODEL`) — currently author and reviewer use the same endpoint, which pxx correctly warns about

Not yet exercised end-to-end:
- 🟡 `dx verify-gui` against a real GUI (code path works and is unit-tested, but has not been demonstrated against a live desktop)
- 🟡 `dx merge` green happy path with a **real** signature (unit-tested green; needs a fresh RL-010 signature to demonstrate)

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `error: externally-managed-environment` on `pip install` | Ubuntu 24.04+ PEP 668 refuses bare pip | activate a venv first (see §2) |
| `dx: command not found` | venv not activated in this shell | `source .venv/bin/activate` or run `.venv/bin/dx` directly |
| `[MODEL_UNAVAILABLE] ... /v1 returned HTTP 404` | manifest endpoint has trailing `/v1` | strip it — pxx appends its own `/v1/models` (see §3) |
| `[MODEL_UNAVAILABLE]` on vLLM but Ollama works | `provider` field missing → defaults to `ollama` → probes `/api/tags` which vLLM lacks | add `provider: "vllm"` in the manifest |
| `[HOOKS_MISSING] run_shell in permission mode 'edit' requires a shell safeguard` | pxx post-edit shell verify, fail-closed | `export PXX_ALLOW_UNGATED_SHELL=1` OR configure a PreToolUse hook |
| `dx merge` says "Stale signature (RL-003)" | ledger head advanced after the signature was made | expected behavior; re-sign against current head |
| `dx doctor` red ⚠️ on network probe | manifest host/port unreachable — expected until you replace the placeholder seed | verify with `curl -s http://<host>:<port>/v1/models` or ping |
| `ERROR: role cards not found at …` | role cards are not where dx looked | set `DX_ROLES_PATH`, or add `roles_path:` to the manifest |
| `gui_verification.vlm_endpoint is not set …` | GUI verification is unconfigured | fill in `gui_verification:` — there is deliberately no default host |
| `signature rejected (RL-003): the signing key has been revoked` | approval made with a retired key | re-sign with a currently-valid key registered in `docs/keys/` |

---

## 11. What you actually have

- A working control plane (`dx`) on your driver box
- Real code generated by your own GPU (Qwen3.8-27B-FP8), no cloud call
- Three RL-003 signature checks that mechanically enforce separation of duties
- A test suite that holds those gates closed, runnable with no lab and no keyring
- An honest snapshot of what's stubbed and why

What you don't yet have (by design, not accident):
- Ledger rows appended automatically on merge
- Actual git merges under MERGE_LOCK
- A green happy-path merge trace

Those unlock when DevSwarmX Gate 1 finishes its baseline runs. Until then, you have a factory that fails safely and can defend every claim it makes.

The lesson worth carrying out of 0.3.0: **a gate without a test is a claim, not a gate.** Three of these gates were failing open in a release that had been hand-verified and written up as working. Writing the tests is what found them.

---

*Re-validated end to end on 2026-09-08 at dx `0.9.0`, from a Surface Pro 6 running
Ubuntu 24.04 in WSL2 against a vLLM endpoint (Qwen3.8-27B-FP8) on the LAN. Every
transcript in §4–§8 was re-run and re-captured at this version.*

*Two of them were wrong, and both were wrong because of changes made in the same
day they document.* §7 told you to run `dx merge T-0007`, a task that exists only
in a private operational ledger; once `0.7.0` repointed the default to the public
reference ledger, every new reader got `queue file not found` from the section
demonstrating the project's central gate. §6 quoted `❌ pxx task failed.` and an
exit code of `2`, both superseded by `0.7.1` — which changed that code to `3`
precisely so a failing task could not be mistaken for a governance refusal. A
tutorial quoting the old line teaches the old contract.

*Neither was caught by 337 passing tests, because nothing checked them. Both are
now machine-checked: `TestMergeTranscriptFidelity` re-runs `dx merge` and requires
the shown output, refuses a task id absent from the default ledger, and pins §6's
failure line and exit code to what `cmd_run.py` actually emits. The `dx roles list`
block in §5 and the version string were already pinned this way, which is why they
did not drift.*

*No fabrication. The only edit is the host-address substitution declared at the top.*

*What this tutorial does **not** establish: that `dx merge` performs a git merge or appends to the ledger (it does not — it verifies and stops), or that `dx verify-gui` has been run against a live desktop (it has not). `dx run` does now write evidence bundles, but a bundle is tamper-evident, not signed — it proves nothing was altered after the fact, not who produced it. The merge gate does now pass all-green against a real GPG signature, but in `tests/test_gpg_integration.py` against committed keys and a synthetic ledger — not against the live `devswarm-ledger` with a freshly-made RL-010 signature, which remains undemonstrated.*
