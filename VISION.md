# dx-orchestrator — Vision

## Governing equation

    output = min(generation, verification)

Generation scales with compute and money. Verification scales only with accountable human attention. The primary metric is **merged, gate-passing changes per hour of team attention**.

`dx` is the kernel that enforces this equation across a fleet of AI agents and inference nodes.

## Seven-pillar architecture

| Pillar | Component | Role |
| --- | --- | --- |
| 1. Compute fabric | DGX mesh + T5810 (A4500s) + asrock (RTX 5060 Ti) + M4 Studio/Mini + Orin Nano | Raw inference and memory |
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

- **Phase 1 (now)** — control plane on a low-power driver (2011 Mac Mini / Surface Pro 6 WSL2). Software-only. All inference routed to lab. No KVM required — SSH screenshot fallback for GUI verification.
- **Phase 2** — move driver to XPS 9510 (i7-11800H, RTX 3050 Ti 4 GB) for faster CLI response and optional local 3B model for prototyping.
- **Phase 3** — Openterface Mini-KVM lands. Add framebuffer hash to `dx merge` as a second GUI gate alongside the Qwen VL semantic check.
- **Phase 4** — Team mode: second GPG key registered in the ledger. `dx merge` enforces Author ≠ Signer at the Git level.

## Known gaps (to design and build)

- **`dx run` role injection** — spec drafted, needs wiring into `pxx` subprocess env.
- **`dx merge` GPG verification** — stub only; `gpg_utils.py` needs to actually verify detached signatures against the ledger head.
- **PSOperator process-separated mode on Orin** — need a systemd/OpenRC unit so observer/gatekeeper/executor start on boot.
- **Orchestration daemon** (`code-review-framework` R13 bounded retries) — not yet implemented as a service.
- **Baseline harness** — `dx baseline-run` for the A/B/C sovereign wave comparison (T5810 vs DGX vs asrock).
- **KVM framebuffer verifier** — waiting on hardware.

## What NOT to build

- No cloud fallback. The whole point is sovereignty; a cloud escape hatch would erode the invariant.
- No LLM-driven gates. Every gate is regex, YAML, or GPG — never a prompt.
- No central dispatcher. Concurrency lives in Git merge conflicts.
- No archive/backup folders in the project footprint. Housekeeping is a first-class concern.
