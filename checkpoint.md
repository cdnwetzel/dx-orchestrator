# checkpoint.md

**Last updated:** 2026-09-08
**Working directory:** `/home/cwe/ai/dx-orchestrator`
**Version:** 0.7.0

## Where we are

`dx-orchestrator` is public, MIT, and working end-to-end on this box
(Ubuntu 24.04 WSL2, Python 3.12). 0.3.0 gave it a licence, a hermetic test suite
and CI; 0.4.0 raised the floor for outside review — real-key GPG proofs,
`mypy --strict`, and documentation the suite refuses to let drift.

**Verified working (re-checked at 0.4.0):**

| Check | Result |
| --- | --- |
| `pytest` | 327 passed |
| coverage | 91% (CI floor 88%) |
| malformed input | stops the line with a message, never a traceback |
| `ruff check .` | clean |
| `mypy src/dx --strict` | clean, 15 files |
| `python -m build` + `twine check` | passes, LICENSE ships in the wheel |
| `dx --version` | `dx 0.6.2` |
| `dx doctor --no-network` | 8/8 green |
| `dx roles list` | 38 cards (11 High / 20 Partial / 7 Anchored) |
| corrupt role card | fails validate, doctor and run (was: silently dropped) |
| invalid role card | `dx run` refuses to be governed by it |
| `dx roles validate` | 38/38 pass |
| `dx merge T-0007` | correctly rejects on stale head (RL-003), exit 1 |
| `dx run … --dry-run` | routes per manifest (endpoint + model + provider) |
| Live `dx run` against the vLLM node | re-run 2026-09-08 at 0.6.1: generated working code, exit 0 |

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
├── CHANGELOG.md                  — 0.1.0 → 0.4.0
├── SECURITY.md                   — trust boundaries + disclosure
├── CONTRIBUTING.md               — gates, fixture rules
├── RELEASE_READINESS.md          — cross-repo MIT/public readiness scan
├── checkpoint.md                 — this file
├── pyproject.toml                — metadata, deps, ruff + pytest config
├── .github/workflows/ci.yml      — ruff, pytest 3.11–3.13, build
├── scripts/setup_dependencies.sh — idempotent installer
├── src/dx/                       — 14 modules (see README)
└── tests/                        — 327 tests, hermetic fixtures + real GPG keys
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

## Go-live status — GO (2026-09-08, v0.6.2)

Reviewed for public peer review and cleared, with scope stated.

**Cleared on evidence, not assertion:**

| | |
| --- | --- |
| Clean clone passes cold | ruff, `mypy --strict`, 327 tests, 91% coverage — verified from a fresh `git clone` |
| RL-003 gate | proved against real revoked and expired GPG keys, not captured transcripts |
| Live run | `dx run` generated working code on lab hardware at 0.6.2, exit 0 |
| Privacy | 0 lab addresses in the tree **or** the full git history |
| Legal | MIT, LICENSE ships inside the wheel |
| Docs | version, env-override table, command table, tutorial transcripts and test count all machine-checked |
| CI | 6 jobs, Python 3.11/3.12/3.13, GPG tests asserted to run rather than skip |

**Scope of the GO.** Ready for peer review of *the control plane and its gates*.
Not ready to be described as a working end-to-end factory — two pipeline stages
are deliberately stubbed, and the repo says so in three places (`README.md`
Status, `TUTORIAL.md` §9 and its closing boundary, `SECURITY.md` "Currently
stubbed").

**Known and stated, not blockers:**
- `dx merge` verifies but does not merge or append to the ledger.
- `dx run` does not write evidence bundles.
- `dx verify-gui` has never run against a live desktop.
- No all-green merge against the *live* ledger with a fresh RL-010 signature
  (the gate does pass all-green against real keys in tests).

**What would flip this to NO-GO:** any of the above being *claimed* as working.
The gates are honest as long as the stubs stay labelled.

## Open-source readiness (scanned 2026-09-08)

Full findings in `RELEASE_READINESS.md`. Summary: reproducing our results takes
**five** repos, not four — `dx` itself plus the four integrations. Three
(`dx-orchestrator`, `pxx`, `psoperator`) are already public MIT; `pxx` resolves
from PyPI at the pinned `2.5.4`, and public `psoperator` is confirmed sufficient
for everything `dx` calls.

Licensing was **not** the blocker: both repos had a single author, so MIT applied
unilaterally — no CLA, no history scrubbing (both were clean of secrets,
addresses and deleted files).

Two things actually block a full public release:

1. ~~**Which role deck is canonical**~~ — resolved 2026-09-08:
   `sdlc-agent-roles` @ `release/v1.1.0`. Strict superset of `claude-sdlc-roles`
   — same 38 cards at the identical path, and MIT-licensed already. Its `main`
   was empty; `release/v1.1.0` was merged to `main` (`058dd88`) and
   **`sdlc-agent-roles` is now PUBLIC under MIT** (2026-09-08), CI green
   including the receipt gate. Its `plugin.json` licence key is deliberately
   deferred — the receipt gate freezes the payload digest, so that field needs a
   real v1.1.1 review round (`RUNBOOK.md` §2). `LICENSE` governs regardless.

   **`dx` migrated to the new deck in 0.7.0** — 18 files, plus a re-captured
   `TUTORIAL.md` transcript.
2. **The ledger should not become a public write surface.** It is hash-chained,
   so it cannot be sanitised without invalidating every signature, and its own
   README argues against publication. Recommended: keep the live ledger private
   and publish a *reference* ledger — schema, verifier, RL-010 key standard, and
   a synthetic green trace. That is all `ledger_utils.py` ever needs.

**Done 2026-09-08:** `sdlc-agent-roles` published (MIT, public);
`claude-sdlc-roles` archived with a pointer to the successor; the reference
ledger built at `~/ai/devswarm-ledger-reference` (13 files, `8c9785e`, cold-clone
verified, **not yet pushed**) with all four RL-003 failure modes proven.

That doc debt is now paid: `dx` 0.7.0 repointed both defaults, dropped `gh` from
the install path entirely, replaced the README's "Before you clone" section, and
re-captured the tutorial transcript. CI now clones the deck and forbids any
skipped test. Remaining: step 12 (the `psoperator` address policy) and the
deferred `plugin.json` licence key.

## Next real work

1. **Wire `TODO(ledger)` in `cmd_merge.py`** — the actual `git merge --no-ff`
   under `MERGE_LOCK.json` plus `SIGNED`/`MERGED` rows appended to
   `devswarm-ledger/ledger.jsonl` in SCHEMA.md canonical form. Deferred until
   DevSwarmX Gate 1 unpauses.
2. **Evidence bundles from `dx run`** — design settled in
   `VISION.md § Reference formats`: schema-per-family (`dx.role_task.v1`,
   `dx.gui_verification.v1`, `dx.merge_gate.v1`), bundle-as-directory with
   README + manifest.json + SHA256SUMS, mandatory `boundary` block.
3. **A green happy-path merge trace against the live ledger.** As of 0.4.0 the
   full gate passes all-green in tests against a *real* GPG signature
   (`tests/test_gpg_integration.py`), so only the live-ledger demonstration
   remains — it needs a fresh signature against the current head, produced
   interactively per RL-010.
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

## Branch protection (2026-09-08)

All five public repos carry an active ruleset on their default branch —
`deletion` + `non_fast_forward`, i.e. the branch cannot be deleted or
force-pushed. Verified by attempting a real force push against
`devswarm-ledger-reference`: rejected with "Cannot force-push to this branch",
remote unchanged.

Direct pushes to the default branch still work — no PR requirement, no required
status checks. That is deliberate for a solo maintainer; the gap this closes is
history rewriting, which is what an append-only hash chain actually needs.
Rulesets are free on public repos, which is why this was unavailable while the
ledger was private (`devswarm-ledger/README.md` flags it as unmitigated on Free).

## Sibling repos

- pxx (PyPI `pxx-orchestrator>=2.5.4`) — https://github.com/cdnwetzel/pxx
- PSOperator — https://github.com/cdnwetzel/psoperator
- sdlc-agent-roles (public, MIT) — https://github.com/cdnwetzel/sdlc-agent-roles
- devswarm-ledger-reference (public, MIT) — https://github.com/cdnwetzel/devswarm-ledger-reference
- claude-sdlc-roles (private, **archived** — superseded by sdlc-agent-roles)
- devswarm-ledger (private — the live operational ledger; stays private)
- camelid (evidence-bundle reference) — https://github.com/cdnwetzel/camelid
