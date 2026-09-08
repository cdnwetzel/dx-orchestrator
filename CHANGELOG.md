# Changelog

All notable changes to `dx-orchestrator`.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-09-07

The repository-solidification release: a licence, a test suite, CI, and the
defects that writing the test suite exposed.

### Security

- **`dx merge` no longer accepts signatures from revoked or expired keys.**
  `gpg --verify` exits 0 and emits `VALIDSIG` for a signature made by a revoked
  or expired key, so gating on the return code let a retired approval key clear
  the RL-003 check. Verification now requires `GOODSIG` and explicitly rejects
  `REVKEYSIG`, `KEYREVOKED`, `EXPKEYSIG`, `KEYEXPIRED`, `EXPSIG` and
  `SIGEXPIRED`, naming the reason in the error.
- **Separation-of-duties enforcement no longer fails open.**
  `SignerIdentity.name` stripped only a `(comment)` from the GPG uid, so a key
  with no comment field kept its `<email>` attached. The comparison against the
  ledger's `author_human` could then never match and an author could approve
  their own task. The name is now reduced to the bare name in every uid form.
- **No hardcoded remote hosts.** `cmd_verify.py` shipped a real lab IP and SSH
  username as fallback defaults, so an unconfigured install would silently SSH
  to another machine. An unset `gui_verification` section is now an error that
  names the manifest it read.
- The manifest seeded by `scripts/setup_dependencies.sh` now contains
  placeholder hosts instead of a real lab topology.

### Added

- `LICENSE` (MIT) — previously claimed in `pyproject.toml` and `README.md` with
  no file present, which left the code all-rights-reserved.
- `tests/` — 146 tests covering role parsing, structural validation, hardware
  routing, the Anchored hard-block, the RL-003 gate (including the all-green
  path), the GPG accept/reject policy, and GUI configuration resolution. The
  suite is hermetic: synthetic fixtures, no private sibling repos, no keyring.
- `.github/workflows/ci.yml` — ruff, pytest on Python 3.11/3.12/3.13, package
  build, and a check that `LICENSE` ships inside the wheel.
- `dx --version`.
- `DX_ROLES_PATH` environment variable and an optional `roles_path:` manifest
  key. The role-card directory was the one external path with no override.
- `DX_VLM_ENDPOINT`, `DX_GUI_SSH_HOST` and `PSOPERATOR_SNAPSHOT_DIR` overrides.
- `RESOURCES.md` — control-plane footprint, host tooling, and per-role-class
  fleet sizing requirements.
- `CHANGELOG.md`.

### Fixed

- **`dx merge` gate verdicts printed out of order when piped.** Failures go to
  stderr (unbuffered) and passes to stdout (block-buffered off a tty), so a
  redirected transcript listed the failure before the checks that preceded it.
  A gate transcript is evidence; every verdict is now flushed as it is decided.
- **`dx run --force` was documented as audit-visible but printed nothing** when
  it bypassed an Anchored role. It now emits a banner naming the role and seat,
  matching `dx merge --force`.
- **`dx merge --force` under-reported what it skips.** It also bypasses GUI
  verification; the help text and banner now say so.
- Manifest and role-registry caches were keyed on nothing, so changing
  `DX_CONFIG` or loading a second role directory in one process silently
  returned the previously loaded data. Both caches are now keyed on their path.
- A role entry specifying only `endpoint` now inherits `model` and `provider`
  from `default` instead of dropping them.
- `PSOperatorClient`'s docstring described an invocation form
  (`python -m examples.run_agent`) that was replaced by an absolute-path call.

### Changed

- `pyproject.toml`: PEP 639 licence metadata (`license = "MIT"` +
  `license-files`), project URLs, trove classifiers, ruff and pytest
  configuration, `ruff` added to the `dev` extra.
- `dx doctor` reports the manifest path it actually resolved rather than a
  hardcoded one, and hints at `DX_ROLES_PATH` when role cards are missing.

## [0.2.0] — 2026-09-07

### Added

- `dx merge` implements the RL-003 signature gate against
  `cdnwetzel/devswarm-ledger`: chain verification via the repo's own
  `tools/verify_chain.py`, detached-signature verification against keys in
  `docs/keys/` using a scratch keyring, payload binding to
  `task_id + ledger_head + role`, and a signer ≠ `author_human` check.
- `ledger_utils.py` — thin helpers over the ledger repo; nothing the ledger
  already ships is reimplemented.
- Anchored roles are hard-blocked in `dx run` (exit 2) with the card's handoff
  text; `--force` bypasses.
- Per-role `PXX_MODEL` and `PXX_PROVIDER` routing alongside `PXX_BASE_URL`.
- `setup_dependencies.sh` preflights an active virtualenv and `gh` auth.

### Fixed

- Manifest endpoints carrying a trailing `/v1` produced `/v1/v1/models` probes
  and a 404 on every task; endpoints are now bare and `provider` selects the
  suffix.
- `dx doctor` network probes switched from `curl -f` (which flags vLLM's
  404-on-`/` as a failure) to raw TCP connect, and are derived from the
  manifest rather than hardcoded.

## [0.1.0] — 2026-09-07

### Added

- Initial control plane: `dx doctor`, `dx roles list|validate`, `dx run`,
  `dx merge`, `dx verify-gui`.
- Role-card parser for the `claude-sdlc-roles` format, structural validator,
  and hardware routing from `~/.config/dx/hardware_manifest.yml`.
- `scripts/setup_dependencies.sh`, `README.md`, `VISION.md`, `checkpoint.md`.

[0.3.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cdnwetzel/dx-orchestrator/releases/tag/v0.1.0
