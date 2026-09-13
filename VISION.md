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
| 3. Constitutional governance | `sdlc-agent-roles` (40 role cards) | Separation of duties, Anchored/Partial/High fit |
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

### Two components disagreeing is a fail-open (0.6.0)

`dx roles validate` rejected a card with an empty mandate. `dx run` accepted the
same card and injected an empty MANDATE and an empty MUST NOT into the prompt.
`dx roles validate` failed on an empty roles directory. `dx doctor` called that
same install healthy.

Neither command was wrong in isolation — each did what its own code said. The
defect lived in the gap between them, and in both cases the *permissive* side is
the one that runs the task. A validator nobody consults before acting is
documentation.

So: **where two components can form an opinion about whether something is
usable, they must share the code that forms it, and the acting one must ask.**
Every check dx performs should be reachable from the command that depends on it,
not merely available in a command an operator might run first.

### The registry is part of the gate (0.5.0)

Every fail-open found so far was in a *check*. This one was in the data the
checks run on. A role card that could not be parsed printed a warning and was
dropped, so the deck silently shrank — and `dx roles validate`, the command
whose entire purpose is to certify the constitution is intact, reported `PASS`
on the remainder.

For an Anchored card the consequence is exact: the card vanishes, so the role is
no longer in the registry, so `dx run` reports "role not found" instead of
refusing to execute it autonomously. The hard-block cannot fire for a role the
registry has never heard of. Corrupting one file turns a governance stop into a
typo message.

The lesson generalises past this bug: **an invariant enforced over a collection
is only as strong as the guarantee that the collection is complete.** Loading
must fail closed, not degrade quietly, and "we parsed 36 of 38" is not a
successful load.

### Documenting a footgun is not removing it (0.5.0)

The trailing-`/v1` endpoint mistake had a tutorial section, a troubleshooting
table row, a warning in the README, and a comment block in the seeded manifest.
It had no check. Every one of those words was written *after* somebody hit it,
and none of them would stop the next person — `dx doctor` pronounced such a
manifest healthy right up to the first `MODEL_UNAVAILABLE`.

When a failure mode is well-understood enough to document at length, that is the
evidence that it should be detected in code.

### A gate that fails badly is a gate that gets bypassed (0.4.1)

`dx merge` on a corrupt ledger printed two green checkmarks and then died with a
`JSONDecodeError` traceback. Every individual check was correct; the *failure
mode* was not. An operator seeing that has three bad options — read a traceback,
assume dx is broken, or reach for `--force`. The third is the one that actually
happens under time pressure, and it is how a mechanically-enforced invariant
becomes optional in practice.

`dx doctor` was worse in a quieter way: it validated the manifest's YAML syntax
and pronounced the install healthy, while `dx run` could not use that manifest
at all. A self-test whose green light does not mean "this works" trains people
to ignore it.

Both are the same class of defect as a fail-open gate, and neither shows up in a
test that only feeds well-formed input. The rule that follows: **a gate is not
finished until its failure output is as designed as its success output** — it
must name the file, the line, and what to do, and it must never make the tool
look broken when the input is what is broken.

### Proving the premise, not just the parser (0.4.0)

The 0.3.0 fix for revoked and expired signing keys was tested against *captured
gpg transcripts*. That proved the parser handled the text correctly; it did not
prove the text was ever produced. The claim underneath — that `gpg --verify`
exits 0 for a signature from a revoked key — was reasoned about, not observed.

It happened to be true. Generating a real key, revoking it, and re-verifying
confirmed exit code 0 with `VALIDSIG` and `REVKEYSIG`, for expired keys too. But
a gate whose justification rests on an unverified assumption about a dependency
is one upstream change away from being wrong silently. The tests now run against
committed real keys, and assert the permissive behavior directly, so a future
GnuPG that tightens it will announce itself.

The general form: **when a gate exists because a dependency is permissive, test
the dependency's permissiveness, not only your handling of it.**

### Documentation is a claim, so check it mechanically (0.4.0)

Six environment overrides were implemented and undocumented; two were documented
before they existed. Both are the same failure as an untested gate — an
assertion nobody verifies. `tests/test_docs_consistency.py` now checks the
mechanically checkable parts: versions agreeing across three files, the override
table matching the implementation in both directions, every subcommand present
in the command table, no routable address anywhere in the package.

It is deliberately narrow. It cannot tell whether prose is *good*, only whether
it is *false* in ways a machine can see. That turns out to cover most of the
ways docs actually rot.

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
