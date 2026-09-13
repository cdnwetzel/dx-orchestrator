# RELEASE_READINESS.md

> **Historical record — this work is done.** Kept because it is the evidence
> behind the 0.7.0–0.8.0 opening-up, not because anything here is outstanding.
> All five repos are public and MIT as of 2026-09-08; `CHANGELOG.md` and
> `checkpoint.md` are the current state.

**Question asked:** identify every repo needed to reproduce our results, scan each,
and cost out making them all public under MIT.

**Scanned:** 2026-09-08, against the working clones in `~/ai/` and the GitHub API.

---

## 1. The repo set — it is five, not four

`README.md` names four *integrations*; `dx` itself is the fifth repo a reproducer
needs. One of the five is consumed from PyPI, not cloned.

| # | Repo | Role in the result | Visibility | Licence | How a reproducer gets it |
| --- | --- | --- | --- | --- | --- |
| 1 | `dx-orchestrator` | control plane, all gates | **public** | MIT | `git clone` |
| 2 | `pxx` | code-generation engine | **public** | MIT | **PyPI** `pxx-orchestrator>=2.5.4` — no clone needed |
| 3 | `psoperator` | desktop automation, GUI verification | **public** | MIT | `git clone` + `pip install -e .` |
| 4 | **`sdlc-agent-roles`** | the 40 role cards `dx` is governed by | **public** ✅ | **MIT** ✅ | `git clone` |
| 5 | **`devswarm-ledger-reference`** | ledger format, verifier, a signed reference trace | **public** ✅ | **MIT** ✅ | `git clone` |

**All five are public and MIT** as of 2026-09-08. The ledger was the one
structural question, and it was answered by splitting: the *format* is published
as `devswarm-ledger-reference`, the live operational ledger stays private. §3
below is that argument, and it is the part still worth reading.

**The licence question itself is easy.** `git log` shows a single author —
`Chris Wetzel <chris@cwetzel.com>` — on every commit of both private repos
(1 commit and 47 commits respectively). No outside contributors, so MIT can be
applied unilaterally: no CLA, no relicensing consent, no third-party copyright
to clear.

---

## 2. Scan results

### 2.1 `dx-orchestrator` — ships today

`327 passed in 3.07s` re-run during this scan. Hermetic confirmed: the suite
needs no private clone, no keyring, no network. MIT, `LICENSE` ships in the wheel.
Nothing to do.

### 2.2 `pxx` — nothing to do

- Public, MIT, 18 stars.
- **PyPI carries exactly `2.5.4`**, the version `pyproject.toml` floors at — the
  dependency resolves for an outsider today. Verified against the index.
- Local `v2` is level with `origin/v2`, working tree clean.
- No tracked `private/`, `.env`, or credential files.

### 2.3 `psoperator` — public and sufficient, but inconsistent with our own red line

Sufficiency is confirmed, not assumed. The public tree carries
`services/observer.py`, `gatekeeper.py`, `executor.py` and the `observer-health`
subcommand that `dx/psoperator_client.py` shells out to. 82 files, MIT. An
outsider cloning the public repo gets what `dx` calls.

**But it publishes lab topology that `dx` forbids in itself.** The public tree
carries real home-fleet RFC1918 addresses, a real device hostname, `orin1`, and a
GPU/host inventory across `presets/`, `config.py`, `tests/test_presets.py` and
`docs/`.

This is deliberate, not an accident: `greptile.json` and `CONTRIBUTING.md`
declare the home /24 and device-model hostnames acceptable operator policy, and
the *work* fleet correctly uses the RFC-5737 documentation range.

The issue is consistency. `checkpoint.md` clears `dx` on "0 lab addresses in the
tree **or** the full git history", and `TUTORIAL.md` has a test that fails the
build if an IP appears. Two repos in one public fleet now hold opposite
standards. Not a licence blocker — a policy call to make on purpose rather than
discover in review.

### 2.4 `claude-sdlc-roles` — one real blocker, three decisions

60 files, 388K, 38 role cards, 1 commit.

**Clean.** No IP addresses, no emails, no credentials, no private key material,
and `git log --diff-filter=D` is empty — nothing was ever deleted, so there is
no history to scrub.

| Blocker | Severity | Fix |
| --- | --- | --- |
| No `LICENSE` file | **hard** | Add MIT, same text as `dx`. Sole author — no consent needed. |
| `.claude-plugin/plugin.json` has no `license` key | minor | Add `"license": "MIT"`. |
| Name is `claude-*` on a Claude Code plugin | minor | Normal practice; add a one-line non-affiliation note. |
| **Which deck is canonical?** | **decide first** | See below. |

**The canonical-deck question is the one that mattered — now resolved.**
`sdlc-agent-roles` @ `release/v1.1.0` is the successor and is a strict superset:
the **same 38 cards at the identical path** (`skills/sdlc-role/roles/`,
byte-identical on spot-diff), plus an **MIT `LICENSE` already in place**, CI,
`SECURITY.md`, `docs/provenance.md`, `receipts/`, and symlinked cross-platform
packaging under `agents/kimi/`. Its first commit is
`chore: initialize public repository`.

So the licence gap in this section applies to the *retired* deck, not the
canonical one. Two consequences: the `dx` migration is a directory rename only
(37 references, 18 files — see the runbook step 7b), and
`claude-sdlc-roles` should be **archived rather than published**.

**But `main` is empty.** All 159 files sit unmerged on `release/v1.1.0`; `main`
holds one commit and zero files. Flipping it public today publishes nothing.
Merge first.

**Scanned 2026-09-08 — clean.** The successor's extra 99 files (`receipts/`,
`review/v1.1.0/`'s 69 review lanes, `docs/provenance.md`, `AGENTS.md`,
`CLAUDE.md`, `KIMI.md`, `SECURITY.md`, 8 scripts, the CI workflow) were read and
pattern-scanned: **0** IP addresses, **0** absolute home paths, **0** URLs, **0**
lab hostnames, **0** credentials, **0** private-repo references, **0** files
deleted in branch history. The only email is `fixture@example.invalid` in a test.
One review transcript mentions a "home-lab endpoint" descriptively, with no
address. It is cleaner than the deck it replaces and needs no redaction.

**One bonus worth naming.** `tests/test_role_parser.py` and
`tests/test_docs_consistency.py` both `skipif` the real 38-card deck is absent —
so those tests **skip in CI today**. Publishing the deck un-skips them, and CI
starts validating the parser against the real cards instead of fixtures only.
That is a genuine strengthening of the public evidence, not just an unblock.

### 2.5 `devswarm-ledger` — the real blocker, and it is not about licensing

22 files, 120K, 47 commits, 21 ledger rows.

**Hygiene is clean.** No IPs, no credentials, no private key material.
`docs/keys/chris.asc` is a *public* key — publishing it is the design intent.
`tools/verify_chain.py` is standalone (`hashlib`, `json`, `sys`, `pathlib` only)
and passes: `OK: 21 row(s) verified.`

The blockers are structural.

**(a) The repo argues against its own publication.** `README.md` states:
"making the repo public is not an option — this is control-plane state." The
reasoning is sound. This is a *live write surface*. Publishing it commits you to
publishing every future task event, `write_set`, and touched repo path, in real
time, forever — because RL-009 forbids deletion. That is an ongoing disclosure
commitment, not a one-time review.

**(b) It cannot be sanitised.** The ledger is hash-chained and the approvals are
GPG detached signatures over the head hash. Editing any row to scrub it breaks
every subsequent hash and invalidates every signature — destroying precisely the
property that makes it worth publishing. RL-009 forbids the rewrite in any case.
So the only options are publish-as-is or do not publish this repo.

**(c) What publish-as-is would disclose.** Low severity individually, but it
should be a decision rather than a surprise:

- `/Users/cwetzel/...` paths, including `.claude.json` and `.claude/settings.json`
  — revealing a second machine and a username
- a `REDLINE` row recording that one task's inputs "look like personal
  communications" (the content is not there; the event is)
- commit SHAs and file paths inside the **private** `DevSwarmX` and `momentum` repos
- the string "firm-pattern remote hard-aborts (RL-005)"
- `chris@cwetzel.com` in the signing-key UID

**(d) It would not be verifiable from outside anyway.** The genesis row anchors
to `github.com/cdnwetzel/DevSwarmX @ 4dfe186` — private. A reader could confirm
the chain is internally consistent but could not check a single row against the
work it claims to attest.

---

## 3. Recommended fix for the ledger — split the repo

**Keep the live ledger private. Publish a reference ledger.**

A new public MIT repo carrying the *format and the tooling*, not the operational
history:

- `SCHEMA.md` — canonical form, chain rule, correction convention
- `tools/verify_chain.py` — already standalone, ships as-is
- `docs/keys/README.md` — the RL-010 key-generation standard
- `queue/README.md`, `approvals/README.md`
- **a fresh synthetic genesis** plus a handful of reference rows that verify
  green, with a demo GPG keypair committed alongside, so `dx merge` runs
  end-to-end for an outsider
- a README saying plainly that the live ledger is private operational state and
  this is the reference implementation

**Why this is the correct fix and not the convenient one:**

1. **It is what `dx` actually needs.** `ledger_utils.py` delegates verification to
   `tools/verify_chain.py` and reads `queue/<task>.json`, `approvals/*.asc` and
   `docs/keys/`. Every one of those is *format*. It never needs our 21 rows.
2. It unblocks reproduction with no permanent disclosure commitment.
3. It closes `checkpoint.md` next-work item 3 — the green happy-path merge trace —
   by providing a public fixture ledger to run it against, instead of waiting on
   an interactive signature against the live head.
4. The private ledger stays intact and unrewritten, so RL-009 holds.

**The quick alternative,** stated second because it is worse: flip the existing
repo public as-is, accepting §2.5(c). It costs nothing and has one real benefit —
branch protection is free on public repos, which closes the force-push gap the
README flags as unmitigated on the Free plan. But it makes every future ledger
row public forever, and that commitment is larger than it looks on the day you
make it.

---

## 4. What it would take

> Each of these twelve steps is expanded into a runbook — exact files, line
> numbers, commands and acceptance tests — at
> `~/ai/open-source-release/RUNBOOK.md`, kept outside the repos because it
> spans five of them and is a one-time migration. Steps 5 and 6 are proven
> there against dx 0.6.2 by `make_reference_ledger.sh`.

| # | Task | Repo | Effort | Blocking? |
| --- | --- | --- | --- | --- |
| 1 | ~~Decide canonical deck~~ → `sdlc-agent-roles`; merge `release/v1.1.0` → `main` | deck | 15 min | **yes** |
| 2 | Add MIT `LICENSE` + `license` key in `plugin.json` | roles | 10 min | **yes** |
| 3 | Add non-affiliation note to README | roles | 5 min | no |
| 4 | Flip roles repo public | roles | 1 min | **yes** |
| 5 | Build the public reference-ledger repo (§3) | new | ~half a day | **yes** |
| 6 | Generate + commit a demo signing keypair and a green reference trace | new | 1 hr | **yes** |
| 7 | Repoint `DX_LEDGER_REPO` default / document both paths | dx | 1 hr | **yes** |
| 8 | Rewrite README "Before you clone" — the private-repo warning becomes wrong | dx | 1 hr | **yes** |
| 9 | Drop the `gh auth` preflight from `setup_dependencies.sh` | dx | 15 min | **yes** |
| 10 | Update `TUTORIAL.md`, `SECURITY.md`, `checkpoint.md` for the new topology | dx | 1 hr | **yes** |
| 11 | Confirm `test_docs_consistency.py` passes after 8–10 (it enforces these claims) | dx | 30 min | **yes** |
| 12 | Decide the fleet-wide lab-address policy (§2.3) | psoperator | decision | no |

**Not on the list:** history scrubbing (both private repos are clean), contributor
consent (single author), third-party licence clearance (all original work), and
anything in `pxx`.

**Critical-path estimate: one focused day**, and step 1 is a decision only you can
make. Steps 8–11 are the hidden cost — `dx`'s docs currently *promise* that two
integrations are private, and its own test suite fails the build when those
promises drift.

---

## 5. Bottom line

Making all five repos public under MIT is **not blocked by licensing**. It is
blocked by one design question
(whether the live ledger should ever be a public write surface — its own README
says no, and it is right).

The honest end state is four public MIT repos plus a fifth public *reference*
ledger, with the live ledger staying private. That reproduces every result in
`TUTORIAL.md` for an outsider, and it does so without committing us to
broadcasting operational state forever.
