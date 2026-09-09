# checkpoint.md

**Last updated:** 2026-09-08
**Working directory:** `/home/cwe/ai/dx-orchestrator`
**Version:** 0.9.0

## Where we are

`dx-orchestrator` is public, MIT, and working end-to-end on this box
(Ubuntu 24.04 WSL2, Python 3.12). 0.3.0 gave it a licence, a hermetic test suite
and CI; 0.4.0 raised the floor for outside review; **0.7.0–0.8.0 made every
dependency public and MIT**, so a stranger can reproduce the results without
asking for access. Release detail is in `CHANGELOG.md` — this file is for
resuming, not for history.

**Verified working (every row re-run at 0.9.0, 2026-09-08):**

| Check | Result |
| --- | --- |
| `pytest` | 375 passed |
| coverage | 91% (CI floor 88%) |
| malformed input | stops the line with a message, never a traceback |
| `ruff check .` | clean |
| `mypy src/dx --strict` | clean, 15 files |
| `python -m build` + `twine check` | passes, LICENSE ships in the wheel |
| `dx --version` | `dx 0.9.0` |
| `dx doctor --no-network` | 8/8 green |
| `dx roles list` | 38 cards (11 High / 20 Partial / 7 Anchored) |
| corrupt role card | fails validate, doctor and run (was: silently dropped) |
| invalid role card | `dx run` refuses to be governed by it |
| `dx roles validate` | 38/38 pass |
| `dx merge T-0001` | all-green against the reference ledger, exit 0 |
| `dx merge` (head moved) | correctly rejects the stale signature (RL-003), exit 1 |
| `dx run … --dry-run` | routes per manifest (endpoint + model + provider) |
| Live `dx run` against the vLLM node | generated working code, exit 0; its own tests pass |
| Evidence bundle from a live run | `dx.role_task.v1` written; `sha256sum -c SHA256SUMS` passes |
| Live `dx run` via `openai-compatible` | exit 0 — dx wires to any OpenAI-shaped stack |
| Live `dx run` with a vendor-style `.../v1` endpoint | exit 0 — the trailing `/v1` is corrected and announced |
| Clean clone from GitHub | 375 tests, ruff, `mypy --strict` all green cold |

**Lab topology is NOT recorded in this repo.** The live routing table is in
`~/.config/dx/hardware_manifest.yml` on each driver box; the manifest seeded by
`scripts/setup_dependencies.sh` contains placeholders only. Keep operational
notes in `LOCAL_NOTES.md` (gitignored).

## Layout

```
dx-orchestrator/
├── LICENSE                       — MIT (matches pxx / psoperator)
├── README.md                     — install + usage + env vars
├── TUTORIAL.md                   — validated walkthrough
├── VISION.md                     — 7-pillar architecture, red lines, lessons
├── RESOURCES.md                  — footprint + fleet sizing requirements
├── CHANGELOG.md                  — 0.1.0 → 0.8.0
├── SECURITY.md                   — trust boundaries + disclosure
├── CONTRIBUTING.md               — gates, fixture rules
├── ROADMAP.md                    — the gap to a full end-to-end run, and how to close it
├── RELEASE_READINESS.md          — cross-repo scan behind the 0.7–0.8 opening-up (historical)
├── checkpoint.md                 — this file
├── pyproject.toml                — metadata, deps, ruff + pytest config
├── .github/workflows/ci.yml      — ruff, pytest 3.11–3.13, build
├── scripts/setup_dependencies.sh — idempotent installer
├── src/dx/                       — 14 modules (see README)
└── tests/                        — 375 tests, hermetic fixtures + real GPG keys
```

## Resume here

```bash
cd ~/ai/dx-orchestrator
source .venv/bin/activate    # or prefix commands with .venv/bin/
pytest && ruff check .
dx doctor                    # --no-network if lab endpoints are unreachable
```

Fresh box (Ubuntu 24.04 / WSL2):

```bash
virtualenv -p python3 .venv   # or python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
./scripts/setup_dependencies.sh
$EDITOR ~/.config/dx/hardware_manifest.yml   # every host in it is a placeholder
```

Every clone the installer fetches is a public repository — no `gh` and no
GitHub credentials are needed.

## Done since 0.2.0

1. ~~Hardware manifest aligned with live topology~~ — done. Endpoint, model and
   provider are all routed per role.
2. ~~Anchored roles hard-block in `dx run`~~ — done, exit 2, `--force` bypasses
   and now announces itself.
3. ~~Doctor probes derived from the manifest~~ — done, raw TCP connect.
4. ~~Live `dx run` against the vLLM node~~ — done; real code generated locally.
5. ~~Licence, tests, CI~~ — done in 0.3.0.
6. ~~Repo visibility / IP scrub decision~~ — resolved: MIT, public, placeholders
   in the shipped seed manifest.

Three real defects surfaced while writing the tests, all fixed in 0.3.0 (see
`CHANGELOG.md § Security`): the separation-of-duties check failed open for GPG
uids with no comment field; revoked and expired keys passed the RL-003 gate; and
`cmd_verify.py` shipped a real host as a fallback default.

7. ~~Peer-review hardening~~ — done in 0.4.0. Coverage 70% → 91%, `mypy --strict`
   across the package, GPG gate proved against committed real keys (usable,
   revoked, expired) rather than captured transcripts, and `prohibited_patterns`
   fixed — it had been returning sentence fragments rather than whole
   prohibitions on every real card.

## Go-live status — GO (2026-09-08, v0.9.0)

Reviewed for public peer review and cleared, with scope stated.

**Cleared on evidence, not assertion:**

| | |
| --- | --- |
| Clean clone passes cold | ruff, `mypy --strict`, 375 tests, 92% coverage — verified from a fresh `git clone` of the public repo |
| RL-003 gate | proved against real revoked and expired GPG keys, not captured transcripts |
| Live run | `dx run` generated working code on lab hardware at 0.8.1, exit 0, and the generated tests pass |
| Privacy | 0 lab addresses in the tree, now guarded tree-wide by `TestNoLabAddressesAnywhere`. **History is not clean:** commit `e066854` wrote the psoperator home range into `checkpoint.md` while documenting the fix for exactly that problem. Removed from the tree; unremovable from history without a force-push the ruleset now forbids. `psoperator` is the same shape. |
| Legal | MIT, LICENSE ships inside the wheel |
| Docs | version, env-override table, command table, tutorial transcripts and test count all machine-checked |
| CI | 6 jobs, Python 3.11/3.12/3.13; GPG **and** real-card **and** merge-transcript tests asserted to run rather than skip |

**Scope of the GO.** Ready for peer review of *the control plane and its gates*.
Not ready to be described as a working end-to-end factory — two pipeline stages
are deliberately stubbed, and the repo says so in three places (`README.md`
Status, `TUTORIAL.md` §9 and its closing boundary, `SECURITY.md` "Currently
stubbed").

**Known and stated, not blockers:**
- `dx merge` verifies but does not merge or append to the ledger.
- `dx merge` still verifies but does not merge or append (ROADMAP §1.2).
- `dx verify-gui` has run against a live desktop once (0.9.1: SSH capture from an
  Xvfb session, checked by a local VLM — YES on a matching expectation, NO on a
  mismatched one), but ROADMAP 1.3 stays open: no `dx.gui_verification.v1`
  bundle is written and PSOperator was not involved.
- No all-green merge against a *live operational* ledger with a fresh RL-010
  signature. The gate does pass all-green against the public reference ledger
  with a real `gpg --verify` — but that ledger's rows attest to no work and its
  demo key was made by a script, which RL-010 forbids for a real approval. The
  gate is proven; the ceremony is not.
- Reproducibility depends on the operator supplying an inference endpoint and a
  pxx shell safeguard. Both are documented; neither ships.

**What would flip this to NO-GO:** any of the above being *claimed* as working.
The gates are honest as long as the stubs stay labelled.

## What changed on 2026-09-08 (detail in `CHANGELOG.md`)

The whole dependency set went public and MIT, and four defects surfaced doing it.

**Every dependency is now public** — `sdlc-agent-roles` (the 38 cards; supersedes
the archived `claude-sdlc-roles`) and `devswarm-ledger-reference` (format,
verifier, RL-010 key standard, and a signed synthetic trace). The live
operational ledger stays private by design: it is hash-chained, so it cannot be
redacted without invalidating every signature, and publishing it would commit us
to publishing all future operational state. `dx merge` needs only the format.

**Defects found, each by running the thing rather than reading it:**

1. `dx run` returned pxx's exit code verbatim, so a failing task could forge
   exit 2 — dx's code for "governance refused this role". Now `3`, guarded.
2. The "no lab addresses" red line was enforced on `TUTORIAL.md` alone, so one
   reached `checkpoint.md`. Now enforced across every tracked file.
3. `TUTORIAL.md` §7 told readers to merge a task that exists only in a private
   ledger; §6 quoted a failure line and exit code superseded the same day. Both
   now machine-checked.
4. `psoperator` published real RFC1918 lab addresses while its own `config.py`
   claimed they were RFC-5737 placeholders. Fixed there; both repos now state
   one policy.

**Branch protection** is active on all five public repos (`deletion` +
`non_fast_forward` on the default branch), verified by a rejected force push.
No PR requirement — the gap worth closing is history rewriting.

## Next real work

**Sequenced, with acceptance criteria and blockers, in `ROADMAP.md`.** The short
version: **§1.1 evidence bundles shipped in 0.9.0**; the ledger append (§1.2) is
next and can be built against the *reference* ledger without waiting on
DevSwarmX Gate 1; the RL-010 ceremony is a scheduling problem, not an
engineering one.


1. **Wire `TODO(ledger)` in `cmd_merge.py`** — the actual `git merge --no-ff`
   under `MERGE_LOCK.json` plus `SIGNED`/`MERGED` rows appended to
   `devswarm-ledger/ledger.jsonl` in SCHEMA.md canonical form. Deferred until
   DevSwarmX Gate 1 unpauses.
2. **Evidence bundles from `dx run`** — design settled in
   `VISION.md § Reference formats`: schema-per-family (`dx.role_task.v1`,
   `dx.gui_verification.v1`, `dx.merge_gate.v1`), bundle-as-directory with
   README + manifest.json + SHA256SUMS, mandatory `boundary` block.
3. **A green merge against a live *operational* ledger.** The gate now passes
   all-green outside the test suite too — `dx merge T-0001` against the public
   reference ledger, real `gpg --verify`, exit 0, and `TUTORIAL.md` §7 shows the
   transcript. What is left is the ceremony, not the gate: a fresh RL-010
   signature made interactively on a trusted terminal, against a ledger whose
   rows attest to real work.
4. **PSOperator process-separated mode on the Orin** — systemd/OpenRC units so
   observer/gatekeeper/executor start on boot.
5. **Orchestration daemon** for `code-review-framework` R13 bounded retries.
6. **`dx baseline-run`** for the A/B/C sovereign wave comparison.
7. **KVM framebuffer verifier** — waiting on Openterface hardware.

## Things NOT to do

- Don't add cloud fallback (sovereignty invariant).
- Don't let the LLM decide any gate (RL-007 — code, not prompts).
- Don't create archive/backup folders inside the project.
- Don't put real lab addresses back into tracked files.
- Don't add a gate without a test. Three fail-open bugs shipped in 0.2.0
  precisely because the gates had no tests.

## Sibling repos

- pxx (PyPI `pxx-orchestrator>=2.5.4`) — https://github.com/cdnwetzel/pxx
- PSOperator — https://github.com/cdnwetzel/psoperator
- sdlc-agent-roles (public, MIT) — https://github.com/cdnwetzel/sdlc-agent-roles
- devswarm-ledger-reference (public, MIT) — https://github.com/cdnwetzel/devswarm-ledger-reference
- claude-sdlc-roles (private, **archived** — superseded by sdlc-agent-roles)
- devswarm-ledger (private — the live operational ledger; stays private)
- camelid (evidence-bundle reference) — https://github.com/cdnwetzel/camelid
