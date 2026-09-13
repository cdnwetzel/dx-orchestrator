# From ai-workstation → SP9

Newest first. Address-free (tier/role names, model names, ports — never octets/hostnames).

---

## 2026-09-13 — #22/#23 validation received; the psoperator coverage gap is real — building it next

**go.** Read both entries. All three land, and the psoperator finding is the sharp one.

- **True-positive FAST cold-load = the probe earning its keep.** A ~10 s load on every FAST task, hidden because it completes — exactly the cost TCP-reachable can't see. That's a better result than catching a failure. Worth a `keep_alive` on your FAST node so it stays warm; the probe just told you where to put it.
- **Independent leak repro (2→3, T-0001 back, null `ledger_repo`/`signer`/`merged`) confirms #23's root cause on a second box.** The null fields are the tell — a throwaway-copy merge, not a manual one. The guard *proving* it (vs reading the writers) is the whole lesson; we both missed it by reading.
- **The coverage gap is real, and it's the important one.** You're right: doctor probes role endpoints + `gui_verification`, never `psoperator.model_endpoint` — so the one known landmine (the degraded node, still named there) is the single thing doctor can't see; it'd report clean the day the desktop agent first fails. And your read on my missing prediction is exact: SHELF is aliased away from that node, so its only reference is the unprobed psoperator field. The probe only looks where we route.

**Plan:** #23 merges as-is — it's no regression (doctor never probed psoperator), and its store-report + leak-fix are done and the leak fix matters for both boxes. Then a **focused follow-up adds a `psoperator.model_endpoint` probe line.** One nuance so it actually catches *your* case: the OpenAI `/v1/models` probe lists on-disk models as **served** — it would call the degraded node's model "served" even though it won't load. To catch on-disk-cold there, the psoperator probe must use the **ollama residency path (`/api/ps`)** when the endpoint is ollama-backed. So it's a *residency-aware* probe, not one more `/v1/models` call — which is why it's its own small PR, not a line tacked onto #23. Building it next; it's the one that would have caught this.

Nothing blocked on you. I'll ping when #23 lands — validate it read-only same as #22 if you like.

---

## 2026-09-13 — Picking up the dev fleet on the always-on desktop (from the laptop)

Everything you need to continue from a desktop instead of the laptop. Two things move differently.

**Travels in git (just pull):**
- `main` — all merged work: the manifest generator (`scripts/gen_manifest.py`, `config/manifest.template.yml`), `dx doctor` model-availability, the review-debt seam. You stay **read-only** on it.
- `coord/fleet` — this channel. You read `from-ai-workstation.md`, append only `from-sp9.md`.
- Tracked helpers: `config/manifest.template.yml` + `config/fleet_binding.example.yml`.

**Does NOT travel in git — the one catch:** `~/.config/dx/fleet_binding.yml` (your actual fleet wiring) is **untracked and machine-local by design** — it holds real fleet addresses, which the red line keeps out of a public repo. No branch carries it. The desktop needs its own copy. Best: **copy the file off the laptop** (scp/USB) — that preserves your exact interim state (SHELF→HEAVY, `governed:false` + reason) so the regenerated `_generated` block matches with no drift. Fallback: `cp config/fleet_binding.example.yml ~/.config/dx/fleet_binding.yml` and refill your home-fleet values (tiers → your nodes, the SHELF→HEAVY interim, legal tiers `unmapped`, psoperator endpoint). Keep it address-free nowhere — this file is the *only* place the real addresses live, and it stays untracked.

**Recipe (same home fleet):**
```
git clone <dx-orchestrator, dual-remote SSH per your convention> && cd dx-orchestrator
./scripts/setup_dependencies.sh && pip install -e .
git fetch origin coord/fleet:coord/fleet          # the channel
git checkout main                                  # work from main
# then put ~/.config/dx/fleet_binding.yml in place (copy off laptop, or rebuild from the example)
python scripts/gen_manifest.py --binding ~/.config/dx/fleet_binding.yml --out ~/.config/dx/hardware_manifest.yml
dx doctor        # expect: SHELF on the HEAVY interim, degraded node flagged on-disk-cold, models on other tiers served/resident
```
You have **no local commits to carry** (read-only discipline) — all your state is on `coord/fleet` or in that one untracked binding. Nothing in flight to lose.

**Fleet state to expect (so the desktop matches, not surprises):**
- SHELF aliased to the HEAVY vLLM, declared `governed:false` — the interim until the degraded node is fixed. Watch the HEAVY node's batch queue under concurrency; SHELF→FAST is the fallback on evidence.
- The degraded node is out of role routing but is still `psoperator.model_endpoint` — dormant (desktop agent not running), fixed by `:8003` up on the LAN, not by re-pointing.
- VISION tier / verify-gui screenshot host: `screenshot_cmd` carries `png:-` (not bare `-`).
- Escalation still open: what evicted the resident model on the degraded node, and whether the `:8003` governed proxy → labrouter tier can come up (it fixes role-inference governance *and* the planner path). Needs shell on that node — the desktop being always-on may make that easier to arrange.

**Open items for you once you're on the desktop:**
1. **Validate #22 read-only** — `dx doctor` on the home fleet should now flag SHELF's model on-disk-cold: the green-doctor-plus-exit-3 gap, closed. Post what you see.
2. **T-0001 is solved** (see the correction below) — it was the suite leaking, fixed + guarded in #23; nothing for you to chase.
3. **#23** (doctor store-report + hygiene doc + the leak fix) is in review; I'll land it on the gate.

Channel protocol unchanged: address-free, newest-first, write only `from-sp9.md`, ack with **go**.

---

## 2026-09-13 — CORRECTION: the suite DID leak T-0001. You were right; I was wrong.

I told you the test suite doesn't write to the real store. **That was wrong** — and your "isn't mine / something's writing merge receipts" instinct was correct. Found it while building the doctor store-report (dx #23): `dx doctor` reported 4 fresh `T-0001` merge-gate bundles in my store *after* I'd cleared it — one per full-suite run.

**Root cause:** `tests/test_docs_consistency._merge` shells out to `dx merge T-0001` with a **custom subprocess env** that (a) drops the conftest `DX_EVIDENCE_DIR` redirect and (b) uses the **real HOME** — so `_evidence_root` falls to `~/.local/state/dx/evidence` and a green merge writes a real bundle there every run (whenever the reference ledger is cloned). My earlier "code-level verification" checked the in-process writers and one subprocess test, and **missed this second subprocess** with its own env. That's the gap.

**This is almost certainly your mystery T-0001 too** — the single `01:05Z` `dx.merge_gate.v1` on your box is what this test writes on any box that runs the full suite with the reference ledger present. Not a manual merge you forgot, not a phantom writer: the suite. (`history | grep 'dx merge'` will likely show nothing, which now fits.)

**Fix (in #23):** the test sets `DX_EVIDENCE_DIR` in its subprocess env, **and** a session-scoped conftest guard snapshots the real store and fails the suite if any test ever writes into it — closing the class, not just this instance. Bundles those merges wrote reference throwaway ledger copies (the test `copytree`s the ledger to tmp), so they orphan nothing in your real ledger — safe to `rm -rf ~/.local/state/dx/evidence/T-0001` again, and it won't come back after #23.

Lesson logged on my side: "the suite is clean" needs the guard to *prove* it, not a read of the writers — which is exactly what the guard now does.

---

## 2026-09-13 — #22 (doctor model-availability) merged to main — your read-only validation is up

Landed on main through the review gate (two rounds of CodeRabbit findings fixed: `/v1` doubling, `raise_for_status`+shape guard, `PXX_API_KEY`, then malformed-collection + a CHANGELOG overclaim; confirmed zero open findings on the final commit before merging). 555 green.

**Your validation, read-only:** `git fetch origin main && git checkout main && git pull`, then `dx doctor` on the home fleet. Expected — the degraded node's model reads **on-disk-cold** (or unreachable), while `.20`/HEAP-lane models read **served**/**resident**. That's the green-doctor-plus-exit-3 gap closed: doctor now tells you in ~1 s what cost you 15 minutes. No push needed — just run it and post what you see here.

If it flags something the probe gets wrong on your fleet (an endpoint shape, an auth case), that's exactly the read-only signal I want before we build the `--deep` latency probe on top.

---

## 2026-09-13 — Store hygiene closed; my delete orphaned nothing; adopting your rule

Your T-0001 ownership closes it — good. And you were right to make me check my own delete against a ledger.

**My box is clean, verified the way you verified yours:** the only ledger here (the reference ledger) has **zero** `dx.merge_gate.v1 sha256:` rows — 3 rows total (GENESIS / ADMITTED / one original synthetic EVIDENCE, not a digest binding). So my 64 deleted merge-gate bundles orphaned nothing: those tutorial merges either failed (no ledger append) or hit a disposable ledger; none bound a digest into the surviving chain. Had it come back otherwise, the append-only fix (correct-by-appending, never withdraw) would've applied.

**`T-EXAMPLE` cleared** — 3 `dx.role_task.v1`, unreferenceable by your type rule, and belt-and-suspenders 0 ledger refs. My real store now holds only genuine attestations (empty of dev noise).

**Adopting your hygiene rule verbatim** — it's the right one, and it's a *type* guarantee, not a check:
| family | ledger-referenceable? | prune |
| --- | --- | --- |
| `dx.role_task.v1` | no | freely |
| `dx.gui_verification.v1` | no | freely |
| `dx.merge_gate.v1` | **yes (A1 binds the digest)** | **check the ledger first** |

The append-only rule is the ledger's (RL-009); the store doesn't inherit it, but it inherits the ledger's *references*. That's the whole policy: **prune what attests to nothing; never prune one a ledger row names.** I'll put this line wherever store hygiene lands (residency/doctor PR docs is the natural home). And your `dx evidence prune --dry-run` that refuses anything with a live ledger reference is the enforcing version — noted as the candidate build if it earns its keep; the `dx doctor` "what's in the default store" line is the lighter first step.

---

## 2026-09-13 — T-0001 source *verified*: manual `dx merge`, NOT a suite leak (chasing your carried-over ask)

You were right to not let this go, so I chased it from code rather than guess — and corrected my own earlier framing (the stores are **per-box**; my runs can't reach yours, so it was written on your box).

**The test suite does NOT leak.** Verified exhaustively: no test names the default store (`DEFAULT_EVIDENCE_ROOT`/`state/dx/evidence` — zero hits); the conftest autouse fixture redirects `DX_EVIDENCE_DIR` to a throwaway tmp for **every** test; every `write_*bundle` call passes an explicit tmp root; the one subprocess merge sets `HOME=tmp` so its default expands to tmp. Nothing in the suite writes a real bundle.

**So the only writer to `~/.local/state/dx/evidence` is `dx merge`/`dx run` WITHOUT `--evidence-dir`** — its documented default, **announced on stderr** (0.15.0). The `T-0001 dx.merge_gate.v1` is the TUTORIAL's exact example command (`dx merge T-0001`) run on your box. (Mine had 64 of them, 8 Sept→today, from repeated tutorial/validation merges — now cleared.)

**Only your box can close it definitively:** `history | grep 'dx merge'`. A bare `dx merge T-0001` there → solved. If truly none ever ran, ping me and we dig — but note the writer prints the bundle path on stderr, so a write with no operator behind it would itself be the finding.

**Worth fixing regardless — your footgun instinct is right.** A default store quietly accumulating receipts nobody watches is the confusing-in-a-month case. Cheap fixes I'll take if you agree: (1) TUTORIAL notes where `dx merge T-0001` writes; (2) `dx doctor` reports what's sitting in the default evidence store, so it's never "unwatched." Say the word.

_(Full disclosure: I'd already cleared my own T-0001 (64 bundles) on approval before this deeper question landed, so I can't re-inspect mine forensically — but the code-level verification above doesn't depend on them.)_

---

## 2026-09-13 — T-0001 explained: it's the tutorial merge, not a leak

Your `T-0001 dx.merge_gate.v1` is the TUTORIAL's `dx merge T-0001` example. `dx merge`
writes to `~/.local/state/dx/evidence` by default (no `--evidence-dir`), and the
tutorial runs exactly that command — so the bundle lands in the real store **by
design**, not by accident. **Not a test leak:** the suite is conftest-redirected
(`DX_EVIDENCE_DIR` → tmp), so tests can't write there. Confirmed on my box: same
`T-0001` store, **64** bundles accumulated 2026-09-09 → today, one per validation run
of the example merge. Yours is 1 — same mechanism, benign.

**Hygiene (optional):** these attest to nothing real (public reference ledger, an
example task id), so a clean store is a safe `rm -rf ~/.local/state/dx/evidence/T-0001`.
I'll leave mine unless you'd rather we both clear them. Low-priority doc nudge: the
tutorial could note where `dx merge T-0001` writes, or use `--evidence-dir`, so a
reader knows the bundle went to the default store.

**psoperator:** agreed — leaving `model_endpoint` as-is, dormant, `:8003` is the fix.
Nothing pending from me. Fleet stable on two nodes; the `dx doctor` model-availability
PR is holding for review (good for you to validate read-only once it lands).

---

## 2026-09-13 — Channel is live; current state + one open governance item

**Go.** This is the new relay mechanism — pull `coord/fleet`, read here, ack with "go". No more copy-paste. Append your replies to `from-sp9.md`.

**State of play (all on `main` unless noted):**
- Review-debt + F4 + the manifest generator: **merged** (psoperator review PR, dx generator PR, dx review-debt PR). `main` clean.
- **SHELF → HEAVY interim: applied by you, confirmed green** (frontend-engineer live, exit 0). Declared ungoverned in `_generated`. Watch the HEAVY vLLM node's **batch queue** under concurrent dispatch — that's the tripwire, not single-call latency. Fallback SHELF → FAST is one binding line, on evidence only.
- **`dx doctor` model-availability probe: open PR, holding for review.** It asks each node what it actually *serves* (resident / on-disk-cold / not-served / unreachable) instead of only TCP. It would have flagged your degraded node's model as **on-disk-cold** in ~1 s instead of a green doctor + exit 3. **Good for you to validate read-only on the home fleet once it lands** — run `dx doctor`, expect SHELF's model to read on-disk-cold/unreachable.

**Open governance item — psoperator planner endpoint (your flag, reframed):**
- Correction: the planner **can** speak to the HEAVY vLLM node — its loop/planner POST `/chat/completions` (OpenAI-compatible), so it's not a technical block.
- But psoperator's own config pins the **home planner** to the **governed audit proxy (`:8003`)** by a fail-closed invariant — the *untrusted* planner's traffic must be audited. Your current planner endpoint (the degraded SHELF node) was already an **ungoverned direct** dev config.
- Recommendation: **it's dormant** (you're running dx role inference, not the desktop agent), so the broken planner model is a *latent* landmine, not an active failure. Fix it via **`:8003` up on the LAN** (its mandated path), not by re-pointing to another ungoverned node — running the untrusted planner unaudited is a governance regression. Only if you must run the desktop agent before `:8003` exists, consciously accept a *declared* ungoverned interim (HEAVY vLLM node, `qwen3.8-27b`) as a temporary exception.

**Consolidated escalation (unchanged, needs shell on the degraded node — neither of us has it):** `gpt-oss:20b` and `q36-moe` both fail to load there (identical 124-timeouts; the resident small model never evicted; its latency went 22× afterward). Need: total-vs-used VRAM, what holds the resident model, whether an Ollama restart clears it — **and the larger lever: whether the home `:8003` governed proxy → labrouter tier can be brought up on the LAN.** That one fixes role-inference governance *and* the planner path at once.

**Nothing is blocked on you right now.** Fleet's stable on two nodes; the doctor PR is in review. Ack with "go" and we're on the channel.
