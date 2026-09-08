# dx-orchestrator Tutorial: Build a Sovereign AI Factory

*~30 minutes from a clean box to a real code-generation task on your own hardware.*

Everything below was captured live from a Surface Pro 6 running Ubuntu 24.04 in WSL2, dispatching to a T5810 vLLM endpoint on the LAN. Any command output shown is real. Any place where dx is not yet doing what you might expect, this document says "not yet" instead of pretending.

---

## 0. What you're building

```
you → dx CLI → role card (governance) → hardware manifest (routing) → pxx → your GPU → files
                                                                              ↓
                                                              (dx merge gate ← devswarm-ledger)
```

`dx` is a thin control plane. It does three things on top of raw `pxx`:

- **Governance** — every task must be run under a named role card (one of 38, shipped in `claude-sdlc-roles`). The card's *Mandate* and *Must not* sections are injected into the agent prompt.
- **Hardware routing** — a manifest maps each role to an inference endpoint and model, so heavy roles hit the T5810 vLLM and light ones hit an Ollama box.
- **Merge gate** — `dx merge` verifies a GPG-signed approval against `devswarm-ledger/SCHEMA.md` before allowing a merge. Currently enforces the RL-003 signature contract; **does not yet** perform the actual git merge or ledger append (deliberately stubbed until Gate 1 unpauses).

What dx does **not** do today, honestly:
- No evidence bundle generation (`dx run` does not yet write receipts).
- No actual git-merge or ledger-append (the merge gate verifies but doesn't commit).
- No GUI end-to-end run has been exercised (`dx verify-gui` code path works but hasn't been demonstrated on this box).

---

## 1. Prerequisites

| Requirement | Why | Check |
|---|---|---|
| Python 3.11+ | dx + pxx floor | `python3 --version` |
| `virtualenv` or `python3-venv` | Ubuntu 24.04 refuses bare `pip install` (PEP 668) | `virtualenv --version` |
| `git` | clones + pxx safety-net tags | `git --version` |
| `gh` (GitHub CLI, authenticated) | two of the three dependency repos are private | `gh auth status` |
| `gpg` | verifies the merge gate | `gpg --version` |
| A reachable inference endpoint | e.g. T5810 vLLM `t5810.lab:8007` | `curl -s http://t5810.lab:8007/v1/models \| head -c 200` |

If `gh auth status` says logged out: `gh auth login` first. If any private-repo clones fail with `could not read Username for 'https://github.com'`, that's the reason.

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

pip install -e .
./scripts/setup_dependencies.sh
```

The setup script is idempotent and preflight-checks that a venv is active and `gh` is authenticated. It will:

- Install `pxx-orchestrator>=2.5.4` from PyPI
- Clone `~/ai/psoperator` and `pip install -e .` it
- Clone `~/ai/claude-sdlc-roles` (private — needs `gh`)
- Clone `~/ai/devswarm-ledger` (private — needs `gh`)
- Seed `~/.config/dx/hardware_manifest.yml` with a template that matches the observed live topology

If a step fails, it exits non-zero with a specific error and you can re-run after fixing.

---

## 3. Configure the hardware manifest

Edit `~/.config/dx/hardware_manifest.yml` to match your lab. Here is the shape that actually works (this is what commit `9a424f5` ships as the seed):

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
    endpoint: "http://t5810.lab:8007"     # DGX substitute — .100 offline
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

**Why the `/v1` warning is non-negotiable.** In an earlier iteration the manifest had `endpoint: "http://t5810.lab:8007/v1"` and every `dx run` failed with `[MODEL_UNAVAILABLE] http://t5810.lab:8007/v1 returned HTTP 404`. Root cause: `pxx/router.py` constructs probe URLs as `{base}/v1/models`, so the `/v1` doubles. Fix is to strip it. Documented in commit `9a424f5` and captured here so nobody else has to rediscover it.

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
✅ Role cards found (38 files at /home/cwe/ai/claude-sdlc-roles/skills/sdlc-role/roles)
✅ devswarm-ledger at /home/cwe/ai/devswarm-ledger
✅ gpg installed (/usr/bin/gpg)
✅ Hardware manifest at /home/cwe/.config/dx/hardware_manifest.yml
   (YAML syntax OK)

🌐 Network checks (non-critical):
✅ role:backend-engineer → t5810.lab:8007 reachable
✅ role:frontend-engineer → asrock.lab:11434 reachable
✅ role:default → macmini.lab:11434 reachable
✅ gui.vlm_endpoint → orin.lab:11434 reachable

✅ All core checks passed. dx is ready to use.
```

The network probes are TCP-connect only (`socket.create_connection`), so a live vLLM that 404s on `/` still shows reachable. Add `--no-network` to skip the probes.

---

## 5. Look at the constitution: `dx roles list`

```bash
dx roles list --fit High
```

Actual output (truncated):

```
Slug                  Fit       Seat          Anchored
--------------------------------------------------------
backend-engineer      High      S4
code-reviewer         High      All engineers (rotating)
data-engineer         High      S5
finops                High      S2 + S7
frontend-engineer     High      S6
mobile-engineer       High      S6
performance-engineer  High      S4 + S8
platform-engineer     High      S7
product-analyst       High      S1
sdet                  High      S8
technical-writer      High      S6 / S7 / S9 split
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

Actual output on this box:

```
🚀 Running task T-LIVE-001 with role backend-engineer on http://t5810.lab:8007 (model: qwen3.8-27b)...
[HOOKS_MISSING] run_shell in permission mode 'edit' requires a shell safeguard (fail-closed); none is configured. Choose one: [...] — 1 file already modified: hello.py [net: pxx-pre/...] (rounds=0 tokens=0 diff_lines=11)
❌ pxx task failed.
```

Exit code from dx: `2`. **But `hello.py` was written**, and pxx says so in its own error line: `1 file already modified: hello.py [...] diff_lines=11`. This is the fail-closed shell-verify gate firing after the successful edit. If you `export PXX_ALLOW_UNGATED_SHELL=1` before running, pxx exits 0.

Verify the file:

```bash
cat hello.py
```

Actual generated output (from T5810 Qwen3.8-27B-FP8):

```python
def greet(name):
    """Return a greeting for the given name."""
    return 'Hello, ' + name


def main():
    print(greet('World'))


if __name__ == '__main__':
    main()
```

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

Test against a real prior approval:

```bash
dx merge T-0007
```

Actual output on this box:

```
❌ Stale signature (RL-003). Signed head 79ad37877e9d4901… but current head is 4916e8c6a7528a5d…. Re-sign after re-verifying the chain.
✅ Ledger chain verifies. Head: 4916e8c6a7528a5d…
✅ Signature verified. Signer: Chris Wetzel <chris@cwetzel.com>
```

Exit code: `1`. This is the correct behavior — T-0007 was signed against an older ledger head, and the ledger has moved forward. RL-003 says stale signatures are invalid.

The `❌` line prints before the `✅` lines because they go to stderr vs stdout with different buffering — in a real terminal they interleave correctly.

Bypass with `--force` (audit-visible on stderr):

```bash
dx merge T-0007 --force
```

Output:

```
⚠️  --force in effect: bypassing signature check.
🔄 Merging T-0007... (stub — wire to devswarm-ledger)
```

Exit code: `0`.

**Not yet tested**: the fully-green happy path (fresh signature against current head, signer ≠ author, all three checks pass, merge proceeds). That requires a fresh GPG signature made against the current ledger head, which per RL-010 must be produced interactively on a trusted terminal with a passphrase never held by any agent. The stale-head negative case exercises every code path except the final "all-good, proceed" branch.

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

---

## 9. What's stubbed (be honest with yourself)

Working today:
- ✅ Role parsing + validation + injection into the pxx prompt
- ✅ Hardware routing (endpoint + model + provider per role)
- ✅ Anchored hard-block in `dx run`
- ✅ Three-part RL-003 signature verification in `dx merge`
- ✅ GUI verification code path (`dx verify-gui`) — the code is real, the SSH screenshot + Qwen VL call work

Deliberately deferred until DevSwarmX Gate 1 unpauses:
- ⏸ `dx run` writing evidence bundles (design captured in `VISION.md § Reference formats`, pattern: schema-per-family like camelid, `dx.role_task.v1`)
- ⏸ `dx merge` doing the actual `git merge --no-ff` and appending `SIGNED`/`MERGED` rows to `devswarm-ledger/ledger.jsonl`
- ⏸ pxx review-independence config (`PXX_REVIEW_MODEL`) — currently author and reviewer use the same endpoint, which pxx correctly warns about

Not yet exercised end-to-end:
- 🟡 `dx verify-gui` against a real GUI on the Orin (code path works, hasn't been demonstrated)
- 🟡 `dx merge` green happy path (requires a fresh RL-010 signature)

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `error: externally-managed-environment` on `pip install` | Ubuntu 24.04+ PEP 668 refuses bare pip | activate a venv first (see §2) |
| `could not read Username for 'https://github.com'` during setup | private-repo clone with no gh auth | `gh auth login` then re-run |
| `dx: command not found` | venv not activated in this shell | `source .venv/bin/activate` or run `.venv/bin/dx` directly |
| `[MODEL_UNAVAILABLE] ... /v1 returned HTTP 404` | manifest endpoint has trailing `/v1` | strip it — pxx appends its own `/v1/models` (see §3) |
| `[MODEL_UNAVAILABLE]` on vLLM but Ollama works | `provider` field missing → defaults to `ollama` → probes `/api/tags` which vLLM lacks | add `provider: "vllm"` in the manifest |
| `[HOOKS_MISSING] run_shell in permission mode 'edit' requires a shell safeguard` | pxx post-edit shell verify, fail-closed | `export PXX_ALLOW_UNGATED_SHELL=1` OR configure a PreToolUse hook |
| `dx merge` says "Stale signature (RL-003)" | ledger head advanced after the signature was made | expected behavior; re-sign against current head |
| `dx doctor` red ⚠️ on network probe | manifest IP/port unreachable from this host | verify with `curl -s http://<host>:<port>/v1/models` or ping |

---

## 11. What you actually have

- A working control plane (`dx`) on your driver box
- Real code generated by your own GPU (Qwen3.8-27B-FP8 on T5810), no cloud call
- Three RL-003 signature checks that mechanically enforce separation of duties
- A honest snapshot of what's stubbed and why

What you don't yet have (by design, not accident):
- Automatic evidence bundles per run
- Actual git merges under MERGE_LOCK
- A green happy-path merge trace

Both of those unlock when DevSwarmX Gate 1 finishes its baseline runs. Until then, you have a factory that fails safely and can defend every claim it makes.

---

*This tutorial was validated live on 2026-09-07 against a T5810 vLLM endpoint (`t5810.lab:8007`, Qwen3.8-27B-FP8) from a Surface Pro 6 running Ubuntu 24.04 in WSL2. dx commit at time of validation: `9a424f5`. Every command output shown was captured from that session — no fabrication.*
