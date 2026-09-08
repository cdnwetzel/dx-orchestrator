# checkpoint.md

**Last updated:** 2026-09-07
**Working directory:** `/home/cwe/ai/dx-orchestrator`

## Where we are

The `dx-orchestrator` repository has been scaffolded, installed, and is functioning end-to-end on this box (Ubuntu 24.04 WSL2, Python 3.12).

**Verified working:**
- Venv at `.venv/` (created with `virtualenv`), `pip install -e .` succeeded
- `pxx-orchestrator 2.5.4` installed
- `~/ai/psoperator` cloned + installed editable (mss, pynput, pydantic, numpy, pillow all resolved)
- `~/ai/claude-sdlc-roles` cloned via `gh repo clone` (38 role cards present)
- `~/.config/dx/hardware_manifest.yml` seeded with default routing table
- `dx doctor --no-network` → all 6 checks green
- `dx roles list` → 38 cards parsed correctly (11 High / 20 Partial / 7 Anchored)
- `dx roles validate` → 38/38 pass
- `dx run <task> --required_role <role> --dry-run` → correct endpoint routing per manifest

**Bugs found and fixed during install:**
- `cmd_doctor.py` and `cmd_run.py` were shelling out to `pxx` by bare name, missing venv-installed binaries. Fixed to prefer `sys.executable`-adjacent binary before PATH.
- `psoperator_client.py` was using `python -m examples.run_agent`, but psoperator's `examples/` is not a package. Fixed to invoke the script by absolute path.
- `role_parser.py` didn't handle claude-sdlc-roles' inline `**Agent fit:** X · **9-person seat:** Y` metadata line (they don't use YAML frontmatter). Now parses both.
- `role_parser.py` didn't match sections like `## Must not (separation of duties)` because normalization produced a longer key. Now does prefix-match on known section names.
- `role_validate.py` seat regex was too strict (rejected real seats like `S8 + borrowed`, `Rotation (S3/S4/S7)`). Loosened to non-empty text.
- `cmd_roles.py` list output hardcoded seat column width to 14, causing overflow. Now sized from data.

### Files present

```
dx-orchestrator/
├── README.md                     — install + usage
├── VISION.md                     — 7-pillar architecture, red lines, phased rollout
├── checkpoint.md                 — this file
├── pyproject.toml                — package metadata, deps, dx entry point
├── .gitignore                    — Python + editor + archive/backup exclusions
├── scripts/
│   └── setup_dependencies.sh     — idempotent installer for pxx, psoperator, roles, manifest
└── src/dx/
    ├── __init__.py
    ├── cli.py                    — argparse entry point (dx = dx.cli:main)
    ├── cmd_doctor.py             — dx doctor: env self-test
    ├── cmd_roles.py              — dx roles list | validate
    ├── cmd_run.py                — dx run: role inject → pxx → optional PSOperator
    ├── cmd_merge.py              — dx merge: GUI verify + GPG stub
    ├── cmd_verify.py             — dx verify-gui: SSH screenshot → Qwen VL
    ├── role_models.py            — RoleCard, FitLevel
    ├── role_parser.py            — Markdown + YAML frontmatter parser
    ├── role_registry.py          — cache of parsed cards
    ├── role_validate.py          — structural checks
    ├── config_loader.py          — hardware_manifest.yml reader
    └── psoperator_client.py      — subprocess wrapper for psoperator CLI
```

## Next steps to resume

Working install on this box is at `~/ai/dx-orchestrator/.venv/`. To use it:

```bash
cd ~/ai/dx-orchestrator
source .venv/bin/activate    # or prefix with .venv/bin/
dx doctor                    # add --no-network if lab endpoints aren't reachable
dx roles list
dx run T-001 --required_role backend-engineer --message "..." --dry-run
```

**To reproduce on a fresh box (Ubuntu 24.04 / WSL2):**
```bash
cd ~/ai/dx-orchestrator
virtualenv -p python3 .venv   # or python3 -m venv .venv if python3-venv is apt-installed
source .venv/bin/activate
pip install -e .
./scripts/setup_dependencies.sh
```
If `claude-sdlc-roles` clone fails via HTTPS (private/404), use `gh repo clone cdnwetzel/claude-sdlc-roles ~/ai/claude-sdlc-roles` — `gh auth` handles the auth.

**Next real work (unblocked):**
1. ~~Edit hardware manifest to match lab~~ — **done 2026-09-07**. Manifest now aligned with live topology (T5810 :8007 vLLM `qwen3.8-27b`, asrock :11434 Ollama `q36-moe:latest`, Orin :11434 `qwen2.5vl:3b`, Mac mini :11434 `deepseek-r1:14b` as default). DGX (.100) confirmed absent from subnet.
2. ~~Anchored roles hard-block in `dx run`~~ — **done 2026-09-07**. `dx run` exits 2 with the card's handoff text; `--force` bypasses (audit-visible).
3. ~~Wire per-role PXX_MODEL~~ — **done 2026-09-07**. `config_loader.get_route_for_role()` returns `(endpoint, model)`, `cmd_run` sets both PXX_BASE_URL and PXX_MODEL. GUI verifier reads `vlm_model` from manifest.
4. ~~Doctor network probes derived from manifest~~ — **done 2026-09-07**. Switched from `curl -f` (which flags vLLM's 404-on-/ as failure) to raw TCP connect via `socket.create_connection`.
5. Live `dx run` (no `--dry-run`) against T5810 — try it now that endpoint + model + network are all confirmed live.
6. Wire `TODO(gpg)` and `TODO(ledger)` in `cmd_merge.py`. Needs a design call: what artifact gets signed? (Options: ledger head SHA, evidence file, or task admission record.)
7. Git init + push to GitHub. Needs decisions: public vs private, whether to scrub lab IPs from the seeded manifest first.

**Confirmed live network path** (verified 2026-09-07 from Surface Pro WSL2):
- macmini.lab Mac mini :11434 ✓
- orin.lab Orin :11434 ✓
- asrock.lab asrock :11434 ✓
- t5810.lab T5810 :8007 ✓ (labrouter :8004 currently down; direct vLLM backend used)

2. **Once psoperator + roles are cloned:**
   ```bash
   dx roles list
   dx roles validate
   ```

3. **Edit `~/.config/dx/hardware_manifest.yml`** to match your actual lab IPs.

4. **First dry-run:**
   ```bash
   dx run T-001 --required_role backend-engineer \
       --message "Write a Python function that returns 42" --dry-run
   ```
   The output should show `PXX_BASE_URL: http://t5810.lab:8004/v1` (or your override).

## Known gaps (from VISION.md)

- **GPG verification in `dx merge`** — currently a stub. Needs to verify detached signature against ledger head and enforce Author ≠ Signer for Partial/Anchored roles.
- **Git ledger append** — `dx merge` prints a placeholder; needs to actually `git merge --no-ff` and append a hash-chained entry to `devswarm-ledger.jsonl`.
- **PSOperator process-separated mode on Orin** — needs a systemd/OpenRC unit so observer/gatekeeper/executor start on boot.
- **Orchestration daemon** for `code-review-framework` R13 bounded retries — not yet implemented.
- **`dx baseline-run`** for the A/B/C sovereign wave comparison — not yet implemented.
- **KVM framebuffer verifier** — waiting on Openterface hardware.

## Things NOT to do

- Don't add cloud fallback (sovereignty invariant).
- Don't let the LLM decide any gate (RL-007 — code, not prompts).
- Don't create archive/backup folders inside the project (housekeeping rule from `~/.claude/CLAUDE.md`).
- Don't run `git init` here without asking the user first — auto-mode caveats apply.

## References in this conversation

- pxx (PyPI `pxx-orchestrator>=2.5.4`) — https://github.com/cdnwetzel/pxx
- PSOperator — https://github.com/cdnwetzel/psoperator
- claude-sdlc-roles — https://github.com/cdnwetzel/claude-sdlc-roles
- portfolio-ai (inference reference) — https://github.com/cdnwetzel/portfolio-ai
