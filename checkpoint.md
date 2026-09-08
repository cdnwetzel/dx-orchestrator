# checkpoint.md

**Last updated:** 2026-09-08
**Working directory:** `/home/cwe/ai/dx-orchestrator`
**Version:** 0.5.0

## Where we are

`dx-orchestrator` is public, MIT, and working end-to-end on this box
(Ubuntu 24.04 WSL2, Python 3.12). 0.3.0 gave it a licence, a hermetic test suite
and CI; 0.4.0 raised the floor for outside review — real-key GPG proofs,
`mypy --strict`, and documentation the suite refuses to let drift.

**Verified working (re-checked at 0.4.0):**

| Check | Result |
| --- | --- |
| `pytest` | 300 passed |
| coverage | 91% (CI floor 88%) |
| malformed input | stops the line with a message, never a traceback |
| `ruff check .` | clean |
| `mypy src/dx --strict` | clean, 15 files |
| `python -m build` + `twine check` | passes, LICENSE ships in the wheel |
| `dx --version` | `dx 0.5.0` |
| `dx doctor --no-network` | 8/8 green |
| `dx roles list` | 38 cards (11 High / 20 Partial / 7 Anchored) |
| corrupt role card | fails validate, doctor and run (was: silently dropped) |
| `dx roles validate` | 38/38 pass |
| `dx merge T-0007` | correctly rejects on stale head (RL-003), exit 1 |
| `dx run … --dry-run` | routes per manifest (endpoint + model + provider) |
| Live `dx run` against the vLLM node | generated working code, no cloud call |

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
├── checkpoint.md                 — this file
├── pyproject.toml                — metadata, deps, ruff + pytest config
├── .github/workflows/ci.yml      — ruff, pytest 3.11–3.13, build
├── scripts/setup_dependencies.sh — idempotent installer
├── src/dx/                       — 14 modules (see README)
└── tests/                        — 300 tests, hermetic fixtures + real GPG keys
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

Private clones (`claude-sdlc-roles`, `devswarm-ledger`) need `gh auth login`
first — the installer preflights this.

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

## Sibling repos

- pxx (PyPI `pxx-orchestrator>=2.5.4`) — https://github.com/cdnwetzel/pxx
- PSOperator — https://github.com/cdnwetzel/psoperator
- claude-sdlc-roles (private) — https://github.com/cdnwetzel/claude-sdlc-roles
- devswarm-ledger (private) — https://github.com/cdnwetzel/devswarm-ledger
- camelid (evidence-bundle reference) — https://github.com/cdnwetzel/camelid
