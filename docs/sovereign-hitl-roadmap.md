# Sovereign HITL Desktop Agent — Roadmap to Full Vision

**Status:** living document. Baseline drafted 2026-09-10; **currency layer updated 2026-09-16** — Phase A closed, Phase B run live, both fleets bound through the generator, the `dx doctor` visibility set shipped, and the hardware witness notes from the Mini-KVM bench recorded. This file is maintained in the dx-orchestrator repo and kept current as phases land.
**Scope:** dx-orchestrator · pxx · sdlc-agent-roles · PSOperator · devswarm-ledger · OpenTerface Mini-KVM / CH9329+UVC
**North star:** a desktop agent for regulated environments — speculative preparation, cryptographic approval, provable incapacity at every layer, evidence an external examiner can verify without trusting any component of the system.

Status markers used below: ✅ shipped · ◑ partially shipped (seam done, more to build) · ☐ not started. A marker names *what* shipped; the exit test is still the authority on *done*.

---

## 0. Vision statement

Violoop-class capability (proactive, cross-app task preparation; hardware-isolated
execution; one-gesture approval) under sovereign, audit-grade governance:

> The agent perceives, prepares, and proposes. It never executes, never approves,
> never graduates a seat. The human approves with a gesture that is also a
> signature. Every consequential act leaves third-party-verifiable evidence.
> No component — including the whole software stack — can fake that evidence.

## 0.5 Progress ledger — shipped as of 2026-09-16

Mapping today's work onto the phases and invariants below. Everything here is on `main` and released, except where noted.

- **RL-010 amended — Decision 0017** (DevSwarmX charter, `devswarm-ledger-reference` in lockstep). The approval key is now hardware-resident, non-exportable, physical-gesture-per-signature; the interactive software ceremony is a *marked, transitional fallback*. "The agent cannot produce this signature" becomes a property of physics, not procedure. **This resolves Open Question 1.**
- **dx 0.19.0 — the approval seam** (tag `v0.19.0`). Three gates, built tests-first:
  - **Residency is a discriminator, not a word** (`dx.approval_key`): `gpg --with-colons --list-secret-keys` field 15 — `+` on-disk, `#` absent, token serial = card stub. Pinned against real gpg output and the documented card format. → **I-5**
  - **Mechanism derived by the verifier, never asserted by the signer** (`dx.staged_action.build_approval_record`): a software key claiming the hardware standard is refused as laundering; the bundle writer rejects any non-derived mechanism. → **I-5**
  - **Four-binding staleness gate** (`dx.staged_action.reverify_bindings` / `execute_approved_stage`): re-verifies ledger head + frame hash + payload hash + bundle hash at execution time; the executor is provably never called when any moved. → **I-4, I-6, Phase C.3**
  - The hermetic **test double is barred by construction** (`docs/keys/test-doubles/`, which the real keyring builder never reads — the 0.10.0 fake_ledger lesson). → **I-5**
- **Phase B staging layer — shipped, B.1 through B.4:** `workflow-stager` + `workflow-operator` Anchored cards; the `dx.staged_action.v1` bundle family; `classify_bundle_risk` (per-action and aggregate); and B.4's full loop — the three-act harness (`dx.staged_harness`, CI-proven) plus the live runner (`examples/staged_harness_live.py`).
- **AT-SPI + reference fixture** (PSOperator): the Linux AT-SPI provider walk (**R-303**, previously a `NotImplementedError` stub) and a GTK invoice fixture (**R-304**) with a shared, importable field-spec. **This resolves Open Question 3 for the Phase B demo** — AT-SPI on a GTK fixture, with the Windows/UIA port deferred as tracked item **D-01** (attestation leads, coverage follows).
- **A1/A4 (earlier this session):** the merge ledger row binds the bundle digest; a cross-repo contract test pins that `dx.observer` and PSOperator agree on the frame hash (the A4 doctrine — test against the real writer, not the re-implementation — which caught the field-15 bug before it could be a false green).
- **The three-act harness — SHIPPED and run live (2026-09-11).** The full loop → receipted rejection → stale refusal ran on opti3090 against the invoice fixture over live AT-SPI + Xvfb: 4/4 fields staged, a real GPG signature, a real hash-chained ledger, and act three's refusal triggered by a real Xvfb window move. The first live `EVIDENCE → REVIEWED → SIGNED → EXECUTED` ledger (12 rows) is committed at `docs/artifacts/first-live-staged-ledger.jsonl`, verifies with `tools/verify_chain.py` (head `0c461c73…`), and is stamped "produced by dx 0.19.0". **This closes Phase B.**

### Shipped 2026-09-11 → 2026-09-14

- **Phase A is closed.** R-203 (authenticated observer envelopes, stateful gate, six attacks fail closed and receipted) and R-205 (deployment isolation) are both ✅ and wired into the live path. The compromised-planner boundary is shut; the hardware witness (Phase E) is now defence-in-depth rather than load-bearing.
- **Review debt cleared to zero** (dx #20, psoperator #10). The substantive one was **F4**: `build_approval_record` now takes a `ResidencyResolver` and *derives* residency from the real signer's fingerprint, closing a laundering hole where a caller could assert `residency="card"` for a software key. The rest were minors — `tail_anchor` exact-identity matching and non-hex rejection, `reverify_bindings` grammar, live-runner provenance. On the psoperator side, the IPC secret is now refused unless owner-only, and `max_nonces` must be a real non-bool int ≥ 1.
- **The fleet-binding generator (dx #21).** `manifest = config/manifest.template.yml (tracked, address-free role→tier) × a per-box binding (untracked, holds the addresses)`. This is what lets two fleets run one codebase without a single address reaching a tracked file. Three properties earned their keep: the manifest is stamped with `_generated.binding_sha256`; a test proves **every** deck card resolves to a tier (no silent fall-through as the deck grows); and a malformed binding exits 2 writing nothing, never a half-manifest.
  - **Governance posture is declared, never silent.** A tier may be `governed: false` + a required reason, or `unmapped: true` + a required reason, and `role_overrides` (role→tier) records a node's deviation from the shared template. All three surface in `_generated`. The point is that an interim unaudited route has to *name itself* in the generated artifact — which is what made the home fleet's ungoverned interim path legible instead of invisible.
- **`dx doctor` learned to see what a node actually serves (dx #22–#25).** TCP reachability was reporting green on a tier that then failed at run time. Now: **availability** (resident / on-disk-cold / not-served / unreachable, via ollama `/api/ps` + `/api/tags`; OpenAI-compatible endpoints report *served* with residency declared opaque); the **evidence store** inventory; the **psoperator planner endpoint**, probed as its own line; and **`--deep`**, which times a 1-token call and flags the slow — worded "degraded **or** under load", because a point-in-time latency cannot distinguish those and should not pretend to.
  - Two findings from this set are worth keeping: the probe caught a ~10 s cold-load on every FAST-tier task that *success had been hiding*; and a fallback path was nearly allowed to re-launder on-disk-cold into "served" when `/api/tags` confirmed ollama but `/api/ps` then failed — now a confirmed-ollama node with a failing `/api/ps` reads UNREACHABLE and never falls back.
- **Evidence-store hygiene (dx #23), and a leak found by proving rather than reading.** `docs/evidence-store-hygiene.md` states the prune rule as a *type guarantee*: `dx.role_task.v1` and `dx.gui_verification.v1` are unreferenceable → prune freely; `dx.merge_gate.v1` is ledger-referenceable (0.18.0/A1 appends an `EVIDENCE` row binding its sha256) → check the ledger first, because deleting one a row names orphans an append-only reference. Separately, the suite *was* writing into the real store: a docs test shelled out with a custom subprocess env that dropped the `DX_EVIDENCE_DIR` redirect. Fixed — and closed as a class by a session-scoped guard that fails the suite if any test writes there. **The lesson is the reusable part: "the suite is clean" was asserted from reading the writers, and was wrong; only the guard settled it.**
- **A narrow guard was the bug, three times over (2026-09-14).** The role-card count guard had already been widened once (README-only → a hardcoded two-file tuple) and left a docstring telling the next author to extend the tuple by hand; nobody did, so four counts across three docs went unchecked and several more were right only by luck. Widening it to every tracked `*.md` then exposed the *second* narrowness — the pattern list itself, which missed a bare `N cards` with no "role", and the hyphenated `N-card deck` — and that in turn exposed the third: the rule "every count must equal today's deck" cannot tell a stale claim from a deliberate record of the past, and it flagged an accurate description of the archived predecessor deck. A count may now be exempted only by a marker that gives a reason, the same **declared-not-silent** posture the generator takes with `governed: false`. The through-line, and the reason this keeps recurring: **a check that needs manual extension is one forgotten edit from silence — and every fix for it has its own narrow axis.**
- **An operational finding that is really a design rule.** A tier bound to a ~37 GB model on a 16 GB card was read for days as a *degraded node* — including by this document's author — when it was simply a model that never fit, thrashing a card it shared with an unnoticed co-tenant. Nothing was broken; a binding was mis-sized. The rule going out of it: **size the model to the card, and never let a latency number assert a cause.** It is why `--deep` reports "degraded or under load" rather than a verdict.

### Shipped 2026-09-15 → 2026-09-16

- **D1 + D2 landed (psoperator #11).** The R-203 frame watermark persists across restarts and nonces are evicted by TTL with a count ceiling, not FIFO-by-count. D3 (a live-loopback kill-switch drill) stays open below.
- **The home fleet is bound, and the generator's declared-not-silent posture paid out on first contact.** The real per-box binding landed (the file itself, not the reconstruction), and of the three edits settled over the channel, *two were already in the file*: the SHELF alias to the heavy vLLM was `governed: false` with its reason written out, and HEAVY/CODE already named the backend slot rather than the router. Only the planner re-point to a model that fits was a real edit. Doctor now reads the planner tier **RESIDENT** where it would have read on-disk-cold under the oversized model — #24 confirming its own fix on the fleet it was built for. The FAST tier's model is pinned resident (`keep_alive: -1`, then made durable in the service environment), which closes the ~10 s cold-load per FAST task that #22 first surfaced — **reproduced twice by different routes** (9 977 ms then 9.21 s, weeks apart, after a re-bind) and invisible both times because the task *completed*.
- **A hardware revision the defaults did not know about (psoperator #12 → #13).** Newer Mini-KVM units ship an MS2109S + CH32V208 (`1a86:fe0c`, native USB CDC, `/dev/ttyACM*`, 115200 fixed) where the published design had a CH9329 behind a CH340 bridge (`1a86:7523`, `/dev/ttyUSB*`, 9600). The frames are identical — verified against the vendor host-app source, whose chip-strategy interface declares no keyboard or mouse methods — so the executor needed no change, but a working unit read as dead hardware. #13 derives port and baud from the detected chip (the same move as RL-010's verifier-derived mechanism, one layer down) and refuses to choose between two attached units. Review then caught the sharper edge: `1a86:7523` is the id of *every* CH340 adapter, so auto-selecting it could have written HID frames into an unrelated device. Resolution: recognise it, never choose it, the operator names the port. A read-only protocol probe as the true discriminator is filed (psoperator #14) and needs the hardware.
- **An operational finding worth its own line:** on macOS, `launchctl kickstart -k` restarts a job without re-reading its plist, and a changed PID looks exactly like a successful reload. Only `bootout` → `bootstrap` → `kickstart` re-reads the file, and the proof is the process environment (`ps eww`), never the plist or the PID. The same entry recorded that a second, curiosity-driven `bootout` on a live node — after the fix was already verified — cost the second of two brief outages. The rule going out of it: **verify from the running process, then stop touching it.**
- **Coordination is now a git channel, and the home-fleet dev seat moved.** Relays between the two fleets run "go → pull → read" over an orthogonal branch that never merges, address-free by rule; the home fleet's dev side has been handed off to a workstation-class box. Both are process, not code, but the channel is where every correction above was caught.

### Still open

- **Phase C.1 hardware — the single parked decision, and the sole remaining blocker on I-5.** Order the touch-sign token and register a second backup key in the same sitting. The seam already binds to the *contract*, so this is a purchase, not a build.
- **One gate-design decision still open** (psoperator; D1 and D2 landed in #11):
  - **D3 — drill realism.** `kill_switch_drill` exercises an in-process gatekeeper, not the deployed IPC path. Recommend one live-loopback drill. Additive, lowest priority.
- **The concurrency sweep** wants an operator window, not a decision — a better experiment now that the home fleet's roles are bound to a `max_num_seqs 4` server.
- **dx role inference has no documented governance posture in either repo** — psoperator's audited-proxy rule is scoped to its own planner lane. This is a **gap, not a violation**, and the honest form of the question is "should dx role traffic be audited, and through what," not "move it off a port." The generator's `governed: false` + reason keeps today's interim declared while it stays open.

## 1. Invariants (bind every phase; a violation anywhere fails the phase)

| # | Invariant | Enforced by | Status |
| --- | --- | --- | --- |
| I-1 | Planner cannot execute | import-path ban + HMAC executor hop (existing) | ✅ |
| I-2 | Model cannot bypass policy | host-enforced gates, hooks exit-2 deny (existing) | ✅ |
| I-3 | Agent never graduates a seat | Fit is static at runtime; all action classes Anchored | ✅ |
| I-4 | Approval binds exact world-state | signature over ledger head + frame hash + payload hash + bundle hash | ✅ `reverify_bindings` (0.19.0) |
| I-5 | Software cannot mint an approval | RL-010 key standard, ceremony, hardware token | ◑ residency gate + derived mechanism + structural double-bar shipped (0.19.0); hardware token pending order |
| I-6 | Ledger cannot be rewritten | RL-009 append-only, hash chain, staleness gate | ✅ staleness gate shipped; append-only existing |
| I-7 | No unattended autonomous execution | T3 hard-block default; away-mode is a non-goal (§8) | ✅ |
| I-8 | Screen content never enters the append-only file | rows carry hashes only; payloads live in evidence bundles | ✅ |

## 2. Gap analysis vs. Violoop (baseline)

| Violoop capability | Our state | Gap class |
| --- | --- | --- |
| HDMI capture + USB-HID, zero host software | UVC capture: parity. HID: the `ch9329` backend names the *wire protocol*, which both chip revisions share; the newer CH32V208 units needed every default around it fixed (psoperator #12 → #13, port and baud derived from the chip; the generic CH340 id is recognised but never auto-selected) | Capture: none. HID: parity restored by #13 — the row overstated it on current hardware until then |
| On-device inference | LAN fleet inference | None functionally; portability note §7 |
| Approval model cannot be bypassed by compromise | planner/executor separation, T3 hard-block, kill switch | Parity (silicon vs. process isolation) |
| **Proactive, screen-aware intent detection** | change detection only (tile diff, pHash keyframes) | **Build (Phase 3)** |
| **Cross-app staged task preparation** | ◑ bundle family + governance seam shipped (0.19.0); live demo pending | **Build (Phases 1–2)** |
| One-gesture approval | ◑ contract + derived mechanism shipped; hardware token + overlay pending | **Build (Phase 2): touch-sign token** |
| Skill acquisition from screen recording | manual trajectory recording | **Build (Phase 3)** |
| Away mode / 24×7 unattended | absent by design | **Non-goal** (violates I-7) |
| (Ours, absent in Violoop) witness chain, ledger, receipts, governance deck | — | Moat, not gap |

## 2.5 Architecture stance — the device is never the brain

Violoop's headline choice is the one to **reject**: it carries an on-device 8B
(Qwen-2.5 Q4 on an RK3576 NPU) as the perception + GUI-navigation + traffic-control
brain, with fine-tuned coordinate→HID navigators and a continuous memory graph,
falling back to cloud (BYO API) for hard reasoning. That design exists because
Violoop has no fleet — an appliance must carry its own brain — and it is the direct
cause of its two weaknesses: the ~70% GUI ceiling (a small vision model doing
coordinate navigation) and a cloud fallback that breaks sovereignty the moment a
task is hard.

We do not share the constraint, so we reject the premise:

- **The device is a witness and an actuator, never the brain.** The Mini-KVM is a
  dumb HDMI capture + HID injector with no NPU and no model. Compute is the
  sovereign LAN fleet.
- **No on-device inference; no cloud fallback.** Fleet-class models on hardware we
  control are both more capable than an edge 8B *and* sovereign — Violoop had to
  choose one; we get both (§6 keeps cloud fallback a non-goal).
- **Accessibility-first grounding, not pixel→coordinate navigation.** We reject the
  vision-coordinate pipeline that ceilings at 70% in favour of AT-SPI/element
  locators with vision as fallback (Phase D.4).
- **Human-gated skills, not an auto-learned memory graph.** An opaque "artificial
  intuition" graph is a liability in a regulated setting; receipted, human-promoted
  skills are the auditable form (Phase D.3).

What we keep from Violoop is the **body, not the brain**: the HDMI-witness + HID
topology and the physical-approval instinct. The on-device model's real benefits —
portability and a fast local perception loop — do not apply here: we are a
human-gated *staging* system, not a fast autonomous vision loop, so a local
perception brain buys nothing the fleet doesn't. The accepted cost is a hard
dependency on reachable sovereign inference — right for regulated on-prem, and why
the self-contained appliance profile is Phase G, deliberately last. A **Mac
Studio (M4 Max, 36 GB)** is a candidate node for that sovereign fleet — it runs
~30B-class quantized models on-device — but note the stance: it would be a *fleet
brain* the KVM device dispatches perception/reasoning to, never the witness/actuator
itself. The compute can be a Mac Studio; the device driving the target stays dumb.

## 3. Phase plan

### Phase A — Close the sovereign known-limits (hardening; no new capability)

These are already named in your own repos' known-limits sections. For
regulated deployment they move from hardening to requirements.

1. ✅ **R-203: authenticate observer envelopes at the gatekeeper.** Gatekeeper
verifies HMAC signature, epoch, key ID, nonce, expiry; rejects rollback and
replay. *Exit: adversarial tests — fabricated envelope, replayed envelope, stale
epoch — all fail closed, each receipted.* **(SHIPPED 2026-09-11.** PSOperator
`gatekeeper/attestation_gate.py` is the stateful gate — the six attacks
(unknown-key, bad-signature, stale-epoch, not-yet-valid, expired, replayed-nonce)
each fail closed, adversary-first tested. It is **wired into the live path**:
`GatekeeperService.handle` authenticates every planner-supplied envelope before
`request_action`, fail-closed, and receipts each verdict into the hash-chained
audit; the service refuses to start without the observer's key. The compromised-
planner boundary is now closed, so the hardware witness (Phase E) is defense-in-
depth rather than load-bearing. Note where it meets R-205: the gate needs the
observer's key, so observer/gatekeeper key-sharing under isolated accounts is an
R-205 decision.)**
2. ✅ **R-205: deployment isolation.** Observer under restricted OS account;
loopback IPC access controls; document the account topology. *Exit: deployment
guide + tests demonstrating the planner account cannot reach observer signing
material.* **(SHIPPED 2026-09-11.** Two-account topology — a trusted *governance*
account runs observer + gatekeeper and owns the owner-only attestation key; an
untrusted *planner* account holds nothing security-critical and reaches the
gatekeeper only over loopback IPC. Observer and gatekeeper share one account
because symmetric HMAC + the key loader's owner-uid/mode-0600 checks make
cross-account key sharing impossible — the planner-vs-governance boundary is fully
cut, fail-closed. `docs/deployment-isolation.md` + `tests/test_deployment_isolation.py`.
Deferred **option C**: asymmetric attestation (observer signs private, gatekeeper
verifies public, holds no secret) for full three-way isolation — a crypto
re-architecture, out of R-205's scope, and the natural way to also isolate the
observer from the gatekeeper if ever needed.)**
3. ✅ **Attestation-key ACL parity on Windows.** POSIX ownership/mode checks exist;
Windows currently fails closed — implement ACL verification or document the
supported topology. *Exit: key provisioning verified on all claimed platforms.*
**(SHIPPED 2026-09-11 via the documentation exit.** A support matrix declares
Linux + macOS supported/verified and Windows fail-closed / not-supported —
provisioning and loading refuse rather than trust an unverified NTFS ACL, asserted
by a test. Windows stays deferred as **D-01**: no unverifiable security path for a
platform with no host. `docs/deployment-isolation.md`.)**
4. ✅ **Kill-switch + audit drills.** Periodic scripted engagement of
`psoperator kill` across the topology; verify precedence over freshness,
policy, execution. *Exit: drill receipts in the ledger.* **(SHIPPED 2026-09-11.**
`gatekeeper/drills.kill_switch_drill` + `psoperator kill-drill` engage the stop
against a canary and require KILL_SWITCHED — proving precedence over execution,
policy (a T3 delete canary), and freshness (a stale-frame canary) — leaving a
hash-chained audit receipt, restoring prior switch state, and failing loud
otherwise.)**
5. ✅ **Tail-hash anchoring.** Anchor audit-chain and ledger tail hashes in a
separately controlled store on a cadence (both repos state this need). This is
the line between "tamper-evident" and "tamper-evident to a third party."
*Exit: anchoring rows verifiable by `tools/verify_chain.py` from cold storage.*
**(SHIPPED 2026-09-11.** `dx.tail_anchor` pins a source's tail hash into a
separate, independently-controlled anchor log that is itself
`verify_chain`-verifiable (the exit criterion, tested against the reference tool);
a source rewritten after anchoring no longer matches its anchor. Source-agnostic,
so one anchor log covers the ledger and the gatekeeper audit; a cron caller is the
cadence.)** **Phase A is closed.**

Also shipped in Phase A's spirit this session: ✅ the merge ledger row binds the
bundle digest (evidence, not just event), and ✅ the A4 cross-repo frame-hash
contract test (`dx.observer` vs. the PSOperator writer).

### Phase B — The staging layer (the core build; closes the biggest Violoop gap)

The agent today acts one action at a time on request. The vision needs it to
prepare multi-step, cross-application work and hold it for approval.

1. ✅ **`workflow-stager` role card** (sdlc-agent-roles). Nine-section schema;
Anchored; mandate = observe context, draft candidate actions, stage for
review; must-not = execute, approve, prioritize, re-fire a rejected stage
without changed evidence; failure modes include approval fatigue (rejection
rate > threshold), stale-context staging, re-proposal loops. *Exit:
`validate-cards.sh` green; dx exit-2 refusal exercised.* **(Shipped with the
paired `workflow-operator` card; both Anchored, both refuse `dx run` with exit 2.)**
2. ✅ **`dx.staged_action.v1` bundle family.** Extends the evidence-bundle pattern:
staged sequence (skill trajectory + extracted parameters), per-action risk
class, bundle-level risk assessment, preview rendering data, approval state.
Mandatory `boundary` block, SHA256SUMS, failed/rejected stages receipted too.
*Exit: bundle verifier + tests for wrongly-passing cases (per CONTRIBUTING
doctrine).*
3. ✅ **Bundle-level risk classification** (`classify_bundle_risk`). T0–T3 runs per
action *and* on the aggregate: cumulative diff cap, cross-application reach,
target-app sensitivity, action-count ceilings. A sequence of 100 T1s at a
billing form is T2 in aggregate. Deterministic, fail-closed, keyword policy
is a floor not a ceiling. *Exit: classifier tests incl. aggregate-escalation
cases.*
4. ✅ **Staged skill replay** (the reliability play). Trigger fires on a known
workflow → planner extracts parameters (vendor, total, date) → stages a
recorded-trajectory replay with parameters → approval card shows field-level
diff → one gesture commits → executor replays layered locators. Locator-based
replay sidesteps the ~70% click-accuracy ceiling of pure-vision agents.
*Exit: end-to-end demo — invoice email → billing form — fully receipted.*
**(SHIPPED and run live, 2026-09-11.** The seam (0.19.0: verifier-derived
mechanism + four-binding staleness gate + `execute_approved_stage`), the AT-SPI
provider, and the GTK fixture are tied together by `dx.staged_harness`
(CI-proven) and `examples/staged_harness_live.py`. The receipted end-to-end run
happened on opti3090 against the **invoice fixture** — stage → field-level
preview → sign → re-verify → execute — producing the first live ledger
(`docs/artifacts/first-live-staged-ledger.jsonl`). The richer **invoice email →
billing form** cross-app scenario named in the exit line runs on the same
machinery and is the natural next demo; the loop itself is proven.)**
5. ◑ **Redaction at capture** (observer, before signing). Window-class rules
(password managers, banking tabs, designated private windows) zeroed/blurred
before the envelope is signed, so redaction itself is attested. The pxx-side
analog of `pxx check`, on the perception path. *Exit: redacted-window
fixtures; envelope carries redaction manifest.* **(The bundle carries a
`redaction_manifest` field today; the capture-side zeroing before signing is
the remaining part.)**

### Phase C — Approval UX: one gesture, full ceremony (friction collapse)

The ledger signature is stronger than Violoop's physical key; the goal is to
make it *as easy* without surrendering a property.

1. ◑ **Touch-sign hardware token** (OpenPGP card, YubiKey-class). One touch = one
signature over `ledger head + frame hash + payload hash + bundle hash` = one
ledger row. This collapses RL-010 ceremony toward Violoop's UX while adding what
their key press lacks: identity, payload binding, receipts. **(RL-010 amended to
require exactly this — Decision 0017; the seam binds to the *contract* — a
detached OpenPGP signature from a non-exportable registered key, physical gesture
— not a vendor. Remaining: order the token and register a second backup key.)**
2. ☐ **Approval overlay client.** Local translucent overlay (not a push
notification — the ntfy receiver correctly denies by default today): staged
actions highlighted on the observed frame, field-level granular edits,
single-keystroke commit bound to the token touch. Host-side only; the target
monitor needs the software path for overlays (hardware can't draw).

   **Candidate form factor — a tablet approval console.** A dedicated, cheap
   tablet as a *thin* approval-and-display surface: it renders the staged action
   and the field-level diff and the operator approves with one tap. Android is
   preferred over iPadOS, which is too locked down for anything past a web-app
   approval pane; a Samsung A-class or similar is ample as pure display (confirm
   NFC on the exact unit — it varies).

   **Interaction model — the glanceable multi-stage console.** The reference is
   AgentMax (agentmax.dev): a local-first, at-a-glance surface showing every
   agent session as working / waiting / needs-you, grouped by project, that
   catches the "waiting for your approval" moment and lets the operator jump
   straight to the one that needs them. Our console is the same *shape* — pending
   stages across seats/projects, each flagged by state (an
   `EVIDENCE → REVIEWED` stage awaiting `SIGNED` is a "needs-you" row) — and
   local-first is shared DNA with the sovereignty posture (nothing leaves the
   machine). One decisive difference: AgentMax *surfaces* the approval moment and
   sends you back to the terminal to act; ours makes the console the **signing
   surface itself** — the approval is a token-bound cryptographic act that writes
   a ledger row, not a jump-back-and-type. It is glanceable status *plus* the gate,
   not status alone.

   **Existing prior art we already own — `ai_macropad`.** An Elgato Stream Deck
   control surface (`~/ai/ai_macropad`) already drives a fleet of terminal agents
   with literal **APPROVE / REJECT / STOP** keys: each agent's hooks report state
   into a flat-file registry, a daemon paints the deck (who is working / waiting /
   needs-you), and a key press becomes `tmux send-keys` to the focused session.
   That is the AgentMax pattern in *physical, local-first* form, already built —
   the strongest existing model for this console. It has the same governance gap
   as the tablet: its APPROVE press is convenience routing (it *types* the
   approval), not an RL-010 signature. So the sovereign version is the same key
   press wired to a **token tap** — the Stream Deck is the glanceable gesture
   surface, the OpenPGP token produces the signature and the ledger row. A
   physical, at-a-glance, sovereign approval console is a small step from what
   `ai_macropad` already does (and the `mcp__streamdeck-agents__*` tools already
   bind keys and labels programmatically).

   **The RL-010 line this must not cross:** the tablet is the display and the
   *gesture*, never the signer. The signature must still come from a
   non-exportable OpenPGP key (Decision 0017). Two paths:
   - *Recommended:* pair a USB-C / NFC OpenPGP token — the operator taps it to
     the tablet to sign. The tablet never holds signing material, so a
     compromised tablet still cannot mint an approval, and the examiner story
     (verify against the public keyring) is unchanged.
   - *Alternative:* use the tablet's own secure element (Secure Enclave /
     StrongBox), gesture-gated by biometrics. Those keys are non-exportable, but
     they are platform keys, not OpenPGP — adopting them means widening the
     approval contract to a WebAuthn/FIDO-style attested key. A real decision
     (see §7), not a free swap, and it must preserve third-party verifiability.

   **Separation of duties:** the approver tablet must not also be the actuator.
   OpenTerface can drive the Mini-KVM from a phone/tablet over USB-C, but that
   device is *injecting HID* — the executor path. If both a KVM-control console
   and an approval console are used, they are different devices; one tablet that
   both actuates and approves collapses the planner/executor/approver boundaries
   the whole design rests on.
3. ✅ **Stale-approval semantics.** Approval valid for exactly one world-state:
new frame, new head, or edited payload → stale → re-present, never
auto-retry. Kills the TOCTOU class on the approval barrier. **(Shipped as
`reverify_bindings` / `execute_approved_stage` in 0.19.0; the refusal originates
in the runner's re-check, and names the binding that moved.)**

### Phase D — Proactive speculation & learning (Violoop's headline, our way)

1. ☐ **Trigger detection.** Escalate from change detection to workflow triggers:
window-class + content signatures (invoice attachment + billing app running)
gate LLM invocation. Trigger policy is code, runs before any inference.
In dx terms: the speculative trigger is the narrowest mandate in the deck.
2. ☐ **Approval calibration loop.** Every barrier decision (approve / reject /
field-edit) becomes a labeled datapoint in pxx's `improve triage` — durable
human verdicts, held-out evaluation. This is the anti-fatigue feedback loop:
measure and optimize *stage acceptance rate*, not just task success.
3. ☐ **Skill acquisition from observation.** Record operator trajectories →
segment into parameterized skills → propose to Gatekeeper as staged replays
(human verdict required for promotion — pxx's human-gated promotion
machinery, reused). Never auto-promote; the `improve` platform's canary /
drift-quarantine / rollback lanes apply to skills as versioned artifacts.
4. ◑ **Element grounding over pixels.** Prefer accessibility/OCR element IDs
(already frame-bound in PSOperator); coordinate actions remain the
canvas-app fallback. Track platform coverage honestly (Windows strongest;
AT-SPI and macOS AX incomplete). **(The Linux AT-SPI provider walk shipped this
session — R-303 — so AT-SPI is now implemented, not stubbed; macOS AX remains,
and the Windows attestation port is tracked as D-01.)**

### Phase E — Hardware witness integration (dx Phase 3, refined)

1. ☐ Wire Mini-KVM **HDMI-only** (HID leg unused) as a strictly read-only
out-of-band witness; frames consumed off-box by the driver or a second host.
(CH9329+UVC remains the crash-cart actuator topology for pre-login/BIOS.)
The witness and the actuator are two legs of the *same* device: E.1 uses only
the capture leg (MS2109S, standard UVC/UAC, binds to `UVCCapture` with no work),
so the actuator-leg defaults finding in psoperator #12 never blocked E.1 — worth
stating because it would be easy to read the one as blocking the other. And the
bench rig *is* an actuator (it injects HID): per C.2 it must never double as the
approval surface. Nothing violates that today; recorded before either is
load-bearing, which is the cheap moment.
2. ☐ **Divergence gate.** Software-observed frame vs. hardware frame compared
(perceptual hash / VLM judge on the hardware frame as ground truth — never
raw SHA-256 equality across different capture pipelines; that gate would
only ever fail to disagree). Divergence → fail closed, receipt, ledger row.
**Two tolerance axes, both required.** *Pipeline:* the capture output is MJPEG
or YUV (MJPEG is lossy) and 4K30 in is downscaled to 1080p30 out — independent
reasons hash equality can never hold. *Time:* the capture chip advertises
sub-140 ms device latency at 30 fps, so with frame quantisation the hardware
witness frame and the software-observed frame are of moments up to ~170 ms
apart; on a changing screen they *legitimately* differ. A gate that models only
the pipeline fails closed on healthy captures — the loud-check failure mode
`--deep` was steered away from, arrived at from the other direction. The VLM
judge's calibration record must also note that fine text on a Retina target may
not survive the 1080p downscale.
3. ☐ **Witness rows in the ledger.** KVM frame hash + observer frame hash +
envelope epoch bound into `dx.gui_verification.v1` and the ledger — the
third attestation. The two hashes are of two different moments (E.2's time
axis): the row binds each capture with its own timestamp and never implies
simultaneity. Today neither timestamp exists — the bundle carries only
`generated_utc`, the envelope `issued_at` + epoch, the ledger row one `ts` — so
E.3 adds a KVM capture time and an observer capture time as distinct fields,
**and a clock contract for comparing them** (one clock domain, or a recorded
offset calibration between the witness host and the observer host). The
calibration record is defined before E.3 is built, not during: signed offset
with its direction stated (witness clock minus observer clock), unit
(milliseconds), the time it was measured and the interval it is valid for, and
its uncertainty — so every reader applies the same correction and a cross-host
delta is reproducible. Without that, E.2's skew bound cannot be evaluated or
reproduced from the persisted row. After R-203, the witness is defense-in-depth; before it,
it is load-bearing (Phase A.1 first, or run both concurrently).

### Phase F — Compliance & team scale

1. ☐ **Phase 4 team mode** (per dx ROADMAP): second GPG key, Author ≠ Signer
enforced at Git level. The desktop agent collapses Anchored seats onto one
operator; the ledger + four-question self-check + time separation (sign your
past self's work) is what keeps the invariants honest at n=1 — state this
explicitly in the design docs. **(The 0.19.0 test-double bar and the "register a
second key from day one" note in Decision 0017 are the down payment on this.)**
2. ☐ **External examiner packet.** One command exports: ledger, bundles,
witness frames (hashes), key registration, anchoring receipts — everything an
external auditor needs to verify the chain *without trusting any fleet
machine*. This is the artifact regulated industry actually buys.
3. ☐ **Retention & privacy policy for evidence bundles.** Payloads are
deletable/rotatable (contrast: ledger rows are forever — I-8). Define what
lives where, for how long, under which jurisdiction's rules.

### Phase G — Packaging & portability (optional, last)

A driver-box appliance profile: one-cable deployment for machines the operator
doesn't administer (Mini-KVM target-side, driver box host-side). Fleet presets
already ship in PSOperator with fail-closed address validation — extend that
idiom. This is convenience packaging, deliberately after everything else: the
system's integrity never depends on it.

## 4. Sequencing rationale

- **A before B's trust claims:** staging receipts and witness rows are only as
strong as the envelope authentication underneath them (R-203) and the
isolation around them (R-205).
- **B before D:** proactive staging presumes a staging format worth proposing.
Build the bundle contract first; speculation is a caller of it.
- **C parallel with B:** the overlay and touch-sign token are client-side; they
consume the same bundle format.
- **E after A.1 (or concurrent):** witness is load-bearing only until R-203
lands; schedule accordingly.
- **G never gates anything.**

## 5. Exit criteria for "full vision realized"

1. Agent detects a known workflow, stages a cross-app replay with extracted
parameters, presents a field-level diff, and waits.
2. Operator approves with one touch that writes a GPG-signed, head-bound ledger
row; executor replays locators; every step receipted.
3. A hardware witness frame, independently captured, hashes into the same
verification bundle; divergence would fail closed.
4. An external examiner with cold-storage anchoring receipts and the public key
ring can verify the entire history without trusting any fleet machine.
5. Adversarial suite (fabricated envelope, replayed approval, stale payload,
redaction bypass attempt, kill-switch drill) fails closed with receipts —
each tested for the ways it can *wrongly pass*.
6. Every one of the invariants I-1..I-8 has a named mechanism and a named test.

## 6. Non-goals (write these down; they are positioning)

- **Away mode / unattended execution.** Violates I-7 and the all-Anchored
design. Violoop's core commercial pitch is deliberately not built.
- **Cloud inference fallback.** Sovereignty is the floor, not a feature toggle.
- **Raw-hash frame matching between software and hardware capture paths.**
Different color pipelines guarantee false divergence; perceptual/VLM judging
only. (Note the distinction preserved in the A4 contract test: raw-hash equality
*is* the right comparison for two software paths; it is the *hardware* path where
perceptual judging is required.)
- **Approval by convenience without signature.** A bare key press (Violoop's
model) gates execution but produces no evidence; ours never will.

## 7. Open questions

- ✅ **RESOLVED (Decision 0017):** Touch-sign token model and OpenPGP card
compatibility with the RL-010 key standard. Answer: amend RL-010 to require a
hardware-resident, non-exportable key with a physical gesture per signature;
the software ceremony becomes a *marked* fallback; the harness binds to the
contract, not a vendor.
- ☐ VLM judge for witness divergence: which fleet endpoint, and its own
calibration record (pxx `calibrate` machinery applies).
- ◑ **RESOLVED for the Phase B demo:** macOS/Linux accessibility coverage.
Answer: AT-SPI on a GTK fixture for the Phase B governance demo (the AT-SPI
provider now shipped); the Windows/UIA breadth port is deferred as tracked item
**D-01**, whose rule is that the Windows capture path must re-prove the
frame-hash contract *before* any breadth demo — attestation leads, coverage
follows. macOS AX completeness remains open.
- ☐ Approval-console key model: if a tablet approval console (Phase C.2) uses
its own secure element (Secure Enclave / StrongBox, WebAuthn/FIDO-style)
instead of a paired OpenPGP token, does the approval contract widen to accept
a platform-attested non-exportable key — and can that still be verified by an
external examiner against a public keyring? Default answer for now: pair an
OpenPGP token; the tablet is display + gesture only.
- ☐ Bundle retention clock: per-jurisdiction defaults, or operator policy file?

---

*Drafted from the dx/pxx/sdlc-agent-roles/PSOperator/devswarm-ledger stack review
and the Violoop capability comparison. Every phase names its exit tests in the
house style: test the ways it can wrongly pass, not only the ways it fails. Kept
current as phases land — the currency layer (§0.5 and the status markers) is
updated with each shipping session so tomorrow starts from the map, not the plan.*
