# ROADMAP.md

**From:** a control plane whose gates are proven.
**To:** a factory that runs a task end to end and leaves evidence behind.

**Written:** 2026-09-08 at `0.8.1`. Companion to `checkpoint.md` (where we are)
and `VISION.md` (why). This file is only about the distance between them.

---

## What "full" means

One task, admitted and completed without a human touching the middle, leaving a
chain anyone can check afterwards:

```
task admitted → dx run → evidence bundle → dx verify-gui → dx merge
                                                              ↓
                                        git merge --no-ff under MERGE_LOCK
                                                              ↓
                                          SIGNED + MERGED rows appended
                                                              ↓
                                   chain verifies, signature binds current head
```

Today the first arrow works and the last three do not. `dx run` generates real
code on real hardware and stops; `dx merge` verifies an approval and stops. The
stubs are deliberate and labelled — this is the plan to remove the labels.

**The ordering principle:** nothing here ships without evidence it works, and
nothing claims to work before the evidence exists. Three defects found on
2026-09-08 were all found by *running* the thing, none by reading it. Every
milestone below states its acceptance as an artifact, not an assertion.

---

# Milestone 1 — A receipted run

**Goal:** `dx run` emits an evidence bundle. Unblocks everything downstream,
depends on nothing external, and is the single highest-value item here.

### 1.1 — `dx.role_task.v1` evidence bundles — ✅ SHIPPED in 0.9.0

Design is settled in `VISION.md § Reference formats`, drawn from surveying 111
real bundles in `cdnwetzel/camelid`. Do not redesign it:

- **Bundle is a directory**, not a file: `README.md` (human) + `manifest.json`
  (machine) + `SHA256SUMS` (tamper-evident) + raw artifacts. Verification is
  `sha256sum -c SHA256SUMS` — no PKI.
- **Schema per family**, not one universal shape: `dx.role_task.v1` here,
  `dx.gui_verification.v1` and `dx.merge_gate.v1` later.
- **Stable core**: `schema`, `title`, `source_head`, `generated_utc`,
  `result.passed`, `checks{}` (name → `{ok, path}`), and a mandatory
  `boundary` block stating what the bundle does **not** prove.

The `boundary` block is the part that matters and the part most likely to be
skipped under time pressure. A bundle without it is a claim wearing a receipt's
clothing.

**Acceptance:** a real `dx run` produces a bundle; `sha256sum -c SHA256SUMS`
passes; `manifest.json` validates against the schema; the `boundary` block is
non-empty and its absence fails the build. A test mutates one artifact byte and
requires the checksum step to fail.

**Shipped 2026-09-08 in 0.9.0.** Admission record:
`docs/admissions/T-1101-evidence-bundles.md`. Every acceptance criterion met and
verified: a live run against lab hardware produced a bundle, `sha256sum -c
SHA256SUMS` passed on it, the manifest carries the stable core, the writer
refuses an empty `boundary` (and leaves nothing behind when it refuses), and
mutating one byte of one artifact makes verification fail.

Two decisions worth carrying into §1.2 and the other bundle families:

- **Evidence must not perturb what it observes.** pxx's stdout is *not*
  captured: `cmd_run.py` hands it the inherited fd deliberately, and interposing
  a pipe changes what pxx sees. The bundle records the routed command, the
  resolved endpoint/model/provider, the scope diff and git status instead, and
  the `boundary` block says the transcript is absent.
- **Failed runs get bundles too**, and an unwritable receipt fails the run
  closed at `EXIT_ERROR` — not `EXIT_TASK_FAILED`, which would misreport a
  successful task as a failed one.

### 1.2 — Wire `TODO(ledger)` in `cmd_merge.py` — ✅ SHIPPED in 0.10.0

The real `git merge --no-ff` under `MERGE_LOCK.json`, plus `SIGNED` and `MERGED`
rows appended to `ledger.jsonl` in `SCHEMA.md` canonical form.

This is the first code in `dx` that **writes** to shared state, so it inherits
the constraints the ledger design already carries: append-only (RL-009), lock
acquisition is itself an append, and two concurrent claims collide as a git
conflict where the loser backs off.

**Sequencing trap worth naming now.** Appending a row moves the head, and the
approval signature binds to the head it was made against. So the `SIGNED` row
must be appended *after* verification and the `MERGED` row after the merge, with
the gate re-reading the head between them — or a correct implementation will
invalidate the very signature it just accepted. `TUTORIAL.md` §7 demonstrates
this exact failure on purpose; do not treat it as an edge case.

**Acceptance:** a merge against the reference ledger produces both rows;
`verify_chain.py` passes afterwards; a second concurrent `dx merge` loses the
lock cleanly rather than corrupting the chain; a test proves the stale-signature
gate still fires against the *post-append* head.

**Shipped 2026-09-08 in 0.10.0.** Admission record
`docs/admissions/T-1102-ledger-append.md`, drafted by
`dx run --required_role tech-lead` and amended twice, before and during the work.

Built against the reference ledger exactly as this entry predicted, without
waiting on Gate 1. `src/dx/ledger_writer.py` carries the invariants; `dx merge
--repo <path>` performs the `git merge --no-ff` of the queue file's `sha`.

The sequencing trap is handled by re-reading the head between the two appends,
and there is a test that reusing the pre-append head is refused. Two further
findings, both from running it:

- **A test wrote to the operator's real ledger.** The tutorial-transcript test
  shells out to `dx merge`; harmless while that command only read, a mutation
  the moment it grew teeth. It now runs against a disposable copy, and a full
  suite leaves the ledger repository byte-identical.
- **`fake_ledger` was a fiction that only held while nothing wrote** — a stub
  verifier reporting a head unrelated to its own rows. `append_row`
  cross-checked and refused. The fixture is now a real chain.

### 1.3 — `dx verify-gui` against a live desktop — ✅ SHIPPED (0.12.0 bundle, 0.13.0 observer)

**Acceptance (met):** a screenshot captured from a live session, checked by the
VLM, producing a `dx.gui_verification.v1` bundle with the image as an artifact
and a `boundary` block stating the VLM's confidence is not a proof — 0.12.0,
exercised live on a headless Linux Xvfb display and a headless macOS box
(window-backing-store capture).

**Observer in the loop (0.13.0):** `dx verify-gui --observer` binds a verified
PSOperator observer attestation to the exact frame by hash. Demonstrated live on
the Xvfb host: the observer's `mss` capture and dx's `import` capture of the same
static framebuffer hashed identically, so `frame_hash_matches` held, and the
bundle recorded the signed attestation (key id, epoch, nonce, issued/expires,
signature) — provenance for the pixels, verified, fail-closed on any mismatch.

**Operational remainder (→ §3.1):** the observer was started by hand for the
demonstration. Making it a boot service so `dx doctor` sees it without manual
start is §3.1 — repeatability, not a §1.3 capability gap.

---

# Milestone 2 — Close the honest caps

These are the four things the go-live call names as stated-but-open. Each is
small; leaving them open is what makes the GO honest, and closing them is what
makes it unnecessary.

### 2.1 — The RL-010 ceremony

**The gap is the ritual, not the gate.** The merge gate passes all-green with a
real `gpg --verify` today — but against a reference ledger whose rows attest to
no work, signed by a demo key a script generated, which is exactly what RL-010
forbids for a real approval.

What closes it: a green merge against a **live operational** ledger, with a
signature produced interactively on a trusted terminal, by the accountable
human, with a passphrase no agent ever holds.

**Acceptance:** the transcript, plus the ledger rows, plus a `dx.merge_gate.v1`
bundle. Recorded in `TUTORIAL.md` as a real capture with the demo-ledger version
kept alongside for people without an operational ledger.

**Effort:** under an hour of work; it is a scheduling problem, not an
engineering one. **Blocked by:** 1.2, and a human at a keyboard.

### 2.2 — Git history carries lab addresses

`dx-orchestrator@e066854` and `psoperator@f895f0f` contain real RFC1918 lab
addresses. The trees are clean and guarded tree-wide by
`TestNoLabAddressesAnywhere`; the history is not.

Removing them needs a force-push, which the branch rulesets now forbid and which
is the wrong trade on a published repo — rewriting public history breaks every
clone and every commit reference for a low-severity disclosure of non-routable
addresses.

**Recommendation: accept and state it, which is what both repos now do.**
Revisit only if the addresses ever become sensitive, in which case the honest
move is a fresh repository, not a rewrite. Recorded here so the decision is
visible rather than forgotten.

**Effort:** none. **Status:** closed by decision.

### 2.3 — `plugin.json` licence key in `sdlc-agent-roles`

The MIT grant is in place (`LICENSE`); only the manifest metadata field is
missing. It cannot be added alone: `validate-receipts.sh` binds the payload
digest, so any change to a tracked file demands a new release directory with
admissible receipts from all five required lanes.

**What closes it:** fold the field into the next release that carries a real
review round anyway. Do not spend a review cycle on a metadata field alone.

**Effort:** minutes, inside a release that is happening for other reasons.

### 2.4 — pxx token budget for local OpenAI-compatible endpoints

Upstream. `pxx` treats only `ollama` and `vllm` as "local" when lifting the
`max_tokens` ceiling, so a **local** llama.cpp reached through
`openai-compatible` silently gets the conservative paid-provider cap — same
hardware, tighter limit, no message.

**What closes it:** either widen the local set, or key the decision on the
endpoint resolving to a private/loopback address rather than on the provider
string. The second is more correct and barely harder.

**Acceptance:** a local `openai-compatible` endpoint gets the lifted ceiling; a
remote one does not; both are tested.

**Effort:** half a day in `pxx`. **Blocked by:** nothing.

---

# Milestone 3 — Operational hardening

Not required for "full", required for "unattended".

### 3.1 — PSOperator process-separated mode — 🟡 observer done (opti3090)

systemd/OpenRC units so observer, gatekeeper and executor start on boot rather
than being launched by hand. Prerequisite for 1.3 being repeatable rather than a
one-off demonstration.

**Acceptance:** a reboot leaves all three healthy; `dx doctor` sees the observer
without manual intervention.

**Done for the observer (0.14.0):** on opti3090 the observer runs as an OpenRC
service (`psoperator-observer`, `need xvfb`, `DISPLAY=:99`), so
`dx verify-gui --observer` binds without a hand-start, and `dx doctor` reports
observer health when `PSOPERATOR_OBSERVER_ATTESTATION_KEY_PATH` is set. The
gatekeeper and executor — the input-injection path that `dx run --gui` would
drive — are not wired here; they belong to whichever box runs that path (the
Orin in the original plan), not the GUI-verification host.

### 3.2 — Orchestration daemon

Bounded retries for `code-review-framework` R13. The gate for this is that
retries must not be able to launder a failure into a pass — a retry budget is a
gate and therefore needs a test for the way it can wrongly pass.

### 3.3 — `dx baseline-run`

The A/B/C sovereign wave comparison. Needs 1.1 first: a comparison without
evidence bundles is a benchmark you cannot audit.

---

# Milestone 4 — The phases already declared

From `README.md § Status`, restated here with what each actually needs:

| Phase | Needs | Blocked by |
| --- | --- | --- |
| **2** — faster driver, optional local model | hardware | budget |
| **3** — Openterface Mini-KVM framebuffer hash as a second GUI gate | the device | hardware not yet on hand |
| **4** — team mode: a second GPG key, Author ≠ Signer at the Git level | a second human with an RL-010 key | people, not code |

Phase 4 is worth noting as the one that changes the security argument
qualitatively. Today separation of duties is enforced by comparing two strings
in one person's control. With a second key it becomes a property of the system
rather than a property of the operator's discipline.

---

# Suggested order

```
1.1 evidence bundles ──┬──> 1.2 ledger append ──> 2.1 RL-010 ceremony ──> "full"
                       │            (build against the reference ledger first)
                       └──> 3.3 baseline-run
2.4 pxx budgets ─── independent, half a day, do it while waiting
3.1 PSOperator units ──> 1.3 verify-gui live
2.3 plugin.json ─── rides the next sdlc-agent-roles release
2.2 history ─── closed by decision, no work
```

**Critical path to a full end-to-end run: roughly two focused weeks**, and the
only genuinely external blocker is DevSwarmX Gate 1 for the *live* ledger — which
1.2 does not need, because the reference ledger is writable and was built for
exactly this.

---

# The rule that got us here

Every defect found on 2026-09-08 — a downstream tool able to forge a governance
refusal, a lab address in a tracked file, a tutorial section broken for every
new reader — was found by running the thing, and each was live under a full
green suite. The tests that *did* hold were the ones pinning a claim to what the
code actually emits.

So for everything above: **the gate and its test land together, and the test
covers the way the gate can wrongly pass.** Three fail-open bugs shipped in
0.2.0 because the gates had none, and all three looked fine on the happy path.
