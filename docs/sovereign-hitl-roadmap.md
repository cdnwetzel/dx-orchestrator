# Sovereign HITL Desktop Agent — Roadmap to Full Vision

**Status:** living document. Baseline drafted 2026-09-10; **currency layer updated 2026-09-10** to reflect the RL-010 amendment (DevSwarmX Decision 0017) and the dx **0.19.0** approval seam. This file is maintained in the dx-orchestrator repo and kept current as phases land.
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

## 0.5 Progress ledger — shipped as of 2026-09-10

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
- **Open dx follow-ups:** #3 (StaleStageError malformed-vs-stale), #4 (reverify report all moved bindings), #6 (tutorial count fix + widen the count guard to any tracked file — the 0.7.2 lesson).
- **The single parked decision:** order the touch-sign token and register a second backup key in the same sitting (Phase C.1 hardware).

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
| HDMI capture + USB-HID, zero host software | CH9329+UVC backends; Mini-KVM in hand | None (parity or better) |
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
the self-contained appliance profile is Phase G, deliberately last.

## 3. Phase plan

### Phase A — Close the sovereign known-limits (hardening; no new capability)

These are already named in your own repos' known-limits sections. For
regulated deployment they move from hardening to requirements.

1. ◑ **R-203: authenticate observer envelopes at the gatekeeper.** Gatekeeper
verifies HMAC signature, epoch, key ID, nonce, expiry; rejects rollback and
replay. Until this lands, the compromised-planner boundary is incomplete and
the hardware witness (Phase E) is load-bearing. *Exit: adversarial tests —
fabricated envelope, replayed envelope, stale epoch — all fail closed, each
receipted.* **(dx-side `dx.observer` verifies signature + freshness + frame-hash
binding, fail-closed; the gatekeeper-side verification and full adversarial
suite are the remaining part.)**
2. ☐ **R-205: deployment isolation.** Observer under restricted OS account;
loopback IPC access controls; document the account topology. *Exit: deployment
guide + tests demonstrating the planner account cannot reach observer signing
material.*
3. ☐ **Attestation-key ACL parity on Windows.** POSIX ownership/mode checks exist;
Windows currently fails closed — implement ACL verification or document the
supported topology. *Exit: key provisioning verified on all claimed platforms.*
(See tracked item **D-01** for the Windows capture/attestation port.)
4. ☐ **Kill-switch + audit drills.** Periodic scripted engagement of
`psoperator kill` across the topology; verify precedence over freshness,
policy, execution. *Exit: drill receipts in the ledger.*
5. ☐ **Tail-hash anchoring.** Anchor audit-chain and ledger tail hashes in a
separately controlled store on a cadence (both repos state this need). This is
the line between "tamper-evident" and "tamper-evident to a third party."
*Exit: anchoring rows verifiable by `tools/verify_chain.py` from cold storage.*

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
2. ☐ **Divergence gate.** Software-observed frame vs. hardware frame compared
(perceptual hash / VLM judge on the hardware frame as ground truth — never
raw SHA-256 equality across different capture pipelines; that gate would
only ever fail to disagree). Divergence → fail closed, receipt, ledger row.
3. ☐ **Witness rows in the ledger.** KVM frame hash + observer frame hash +
envelope epoch bound into `dx.gui_verification.v1` and the ledger — the
third attestation. After R-203, the witness is defense-in-depth; before it,
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
