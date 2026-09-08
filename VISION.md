# dx-orchestrator — Vision

## Governing equation

    output = min(generation, verification)

Generation scales with compute and money. Verification scales only with accountable human attention. The primary metric is **merged, gate-passing changes per hour of team attention**.

`dx` is the kernel that enforces this equation across a fleet of AI agents and inference nodes.

## Seven-pillar architecture

| Pillar | Component | Role |
| --- | --- | --- |
| 1. Compute fabric | A mixed local GPU fleet — heavy vLLM nodes, faster Ollama nodes, an edge VLM box | Raw inference and memory |
| 2. Execution hands | `pxx` (code) + `PSOperator` (GUI) | Autonomous actuators — edit files, click, type |
| 3. Constitutional governance | `claude-sdlc-roles` (38 role cards) | Separation of duties, Anchored/Partial/High fit |
| 4. SDLC process & audit | `code-review-framework` (R1–R15) | Findings lifecycle, multi-agent orchestration |
| 5. Conversational commander | `Momentum` (WhatsApp/Slack/SMS) | Async human commands, escalations, daily brief |
| 6. Tactical control surface | `ai_macropad` (Stream Deck) | Real-time human override |
| 7. Control plane (this repo) | `dx` (CLI + Git ledger) | Enforces RL-001..011, routes hardware, gates merges |

## Non-negotiable red lines (RL-001 … RL-011)

RL-007 and RL-010 shape the architecture most directly:
- **RL-007** — Local models never act as pass/fail gates. Every gate in `dx` is code, not a prompt.
- **RL-010** — Approval keys are never harness-reachable. GPG signatures happen outside the agent context.

RL-011 (evidence redacted at capture time) is enforced at merge time, optionally cross-checked against a KVM framebuffer hash when the Openterface arrives.

## Why sovereignty

- All inference runs on owned silicon. No cloud API bills, no rate limits, no data egress.
- The ledger is a hash-chained Git repo. No central dispatcher.
- Every gate is deterministic and mechanically enforceable — the LLM is a worker, not a judge.
- Physical console verification (via Openterface KVM) is the trust anchor when it lands.

## Phased rollout

- **Phase 1 (now)** — control plane on a low-power driver (a 2011 Mac Mini or a Surface Pro 6 under WSL2 both suffice). Software-only. All inference routed to the fleet. No KVM required — SSH screenshot fallback for GUI verification.
- **Phase 2** — move the driver to a faster laptop for quicker CLI response and an optional local 3B model for prototyping.
- **Phase 3** — Openterface Mini-KVM lands. Add framebuffer hash to `dx merge` as a second GUI gate alongside the Qwen VL semantic check.
- **Phase 4** — Team mode: second GPG key registered in the ledger. `dx merge` enforces Author ≠ Signer at the Git level.

## Reference formats (from sibling repos — do not reinvent)

The signing, ledger, and evidence contracts already exist across your other
repos. `dx` must **conform** to them, not compete with them.

- **`cdnwetzel/devswarm-ledger` / `SCHEMA.md`** — the authoritative ledger row
  schema (`ts, task_id, author_seat, author_human, reviewer_seat, action, sha,
  evidence, prev_hash`), canonical form
  (`json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`),
  hash chain rule, and the action enum (`GENESIS | ADMITTED | EXECUTED |
  EVIDENCE | REVIEWED | SIGNED | MERGED | INCOMPLETE | ABANDONED | ESCALATED |
  REDLINE | CORRECTION`). `tools/verify_chain.py` is the reference verifier.

- **Approvals** (same repo, `approvals/<task_id>.<role>.asc` + `.msg`) — GPG
  **detached** signature over the canonical UTF-8 string
  `task_id + ledger_head_hash + role` (exact concatenation, no separators).
  Valid only if the key belongs to the human accountable for the role
  (registered in `docs/keys/`), the signer is not the author when separation
  applies, and the signed head hash is the **current** head (stale signatures
  are invalid — RL-003). Real signed approvals for T-0002/3/4/7 exist in that
  repo as reference examples.

- **`cdnwetzel/camelid` / `qa/evidence-bundles/`** — 111 real evidence
  bundles catalog what "receipted run" actually looks like when you scale
  from one to many. Design lessons from surveying the archive:
    - **Schema-per-family, not one-schema-fits-all.** 40+ distinct schema
      names (`camelid.public_evidence_bundle.v1`,
      `camelid.four_row_compact_current_head_public_evidence.v1`,
      `camelid.backend_q8_stream_diagnostics_loop.v1`, …). Future dx
      receipts should be `dx.role_task.v1`, `dx.gui_verification.v1`,
      `dx.merge_gate.v1` — NOT one universal shape.
    - **Stable core, family-specific tail.** ~150 unique keys observed
      across bundles. The universal set: `schema`, `title`, `source_head`,
      `generated_utc`, `result.passed`, `boundary` (explicit non-claims),
      `checks{}` (name → {ok, path}). Everything else is family-specific.
    - **`boundary` is non-negotiable.** Every serious bundle has an
      explicit "what this does NOT prove." Same discipline as pxx's
      RECEIPTS.md "Boundary" paragraph, in machine form.
    - **Bundle = directory, not file.** README (human) + manifest.json
      (machine) + SHA256SUMS (tamper) + raw artifacts. No PKI needed —
      `sha256sum -c SHA256SUMS` is the verify step.
    - **Directory name is load-bearing.** `<test>-<utc-ts>-head-<sha>/`
      tells you what/when/against-what from the name alone; renaming
      breaks SHA256SUMS.

- **`cdnwetzel/pxx` / `docs/RECEIPTS.md`** — the *claims* register model:
  every public claim is Reproducible or Attested, has a dated record, a
  procedure a stranger can run, and an explicit boundary of what is NOT
  claimed. `pxx/manifest.py` computes an `agent_version_id` content hash so
  "which agent produced this" is deterministic.

- **`cdnwetzel/psoperator` / `docs/attestation.md`** — HMAC-SHA256 signed
  observer snapshots with canonical-JSON body (`key_id`, `observer_epoch`,
  `issued_at`, `expires_at ≤ 60s`, fresh 256-bit `nonce`, frame hash, element
  inventory) and replay-protection primitives.

## Lessons learned

Recorded as they land, so the architecture keeps paying for itself.

### A gate without a test is a claim, not a gate (0.3.0)

`dx` 0.2.0 shipped with the RL-003 merge gate hand-verified and written up as
working. It was demonstrated on the negative path — a stale signature, correctly
rejected — and that was taken as evidence the gate worked. Writing the test suite
found three gates failing **open**:

1. **Separation of duties never fired.** The signer's name was extracted from the
   GPG uid by stripping a `(comment)` field. A uid with no comment kept its
   `<email>`, so the comparison against the ledger's `author_human` could never
   match — and an author could have approved their own task. The key actually in
   use has no comment field.
2. **Revoked and expired keys passed.** `gpg --verify` exits 0 and emits
   `VALIDSIG` for a signature made by a revoked or expired key; the check gated
   on the return code.
3. **`dx verify-gui` shipped a hardcoded host.** An unconfigured install would
   silently SSH to a specific machine and report on *its* screen.

All three are invisible on the happy path and invisible on the negative path that
was demonstrated. Only enumerating the failure modes in code found them.

The consequence for RL-007 is sharper than first stated. "Every gate is code, not
a prompt" is necessary but not sufficient: **code that is never exercised against
its own failure modes is no more trustworthy than a prompt.** A gate is not
delivered until the ways it can wrongly pass are tests.

### Receipts must be readable in the order decisions were made (0.3.0)

`dx merge` printed passes to stdout and failures to stderr. Off a tty, stdout
block-buffers, so a piped transcript listed the failure before the checks that
preceded it. A human reading it in a terminal saw the right thing; a log file
did not. Evidence that only reads correctly interactively is not evidence — flush
at every verdict.

### Publishing forces the topology question early

Making the repo public required removing a real lab topology from the installer,
from code defaults, and from the tutorial. The right home for it was already
there: `~/.config/dx/hardware_manifest.yml`, per-box and untracked. Code that
carries a working default host is code that will eventually talk to the wrong
machine. There are now no remote host defaults anywhere in the package.

## Known gaps (to design and build)

- ~~`dx run` role injection~~ — done; mandate and must-not are injected into the
  `pxx` prompt and the route is passed via `PXX_BASE_URL`/`PXX_MODEL`/`PXX_PROVIDER`.
- ~~`dx merge` GPG verification~~ — done; three RL-003 checks against the ledger,
  rejecting stale heads, mismatched payloads, same-person approvals, and revoked
  or expired keys.
- **`dx merge` ledger write** — still a stub. Needs `git merge --no-ff` under
  `MERGE_LOCK.json` plus `SIGNED`/`MERGED` rows in SCHEMA.md canonical form.
- **Evidence bundles from `dx run`** — the format is settled (see below); nothing
  is written yet.
- **PSOperator process-separated mode on Orin** — need a systemd/OpenRC unit so
  observer/gatekeeper/executor start on boot.
- **Orchestration daemon** (`code-review-framework` R13 bounded retries) — not yet
  implemented as a service.
- **Baseline harness** — `dx baseline-run` for the A/B/C sovereign wave comparison.
- **KVM framebuffer verifier** — waiting on hardware.

## What NOT to build

- No cloud fallback. The whole point is sovereignty; a cloud escape hatch would erode the invariant.
- No LLM-driven gates. Every gate is regex, YAML, or GPG — never a prompt.
- No central dispatcher. Concurrency lives in Git merge conflicts.
- No archive/backup folders in the project footprint. Housekeeping is a first-class concern.
- No real network topology in tracked files. It belongs in the per-box manifest.
- No new gate without tests for the ways it can wrongly pass.
