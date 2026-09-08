# Changelog

All notable changes to `dx-orchestrator`.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.2] — 2026-09-08

### Fixed

- **A real lab address reached a tracked file.** `VISION.md`'s red line is "no
  real lab addresses in tracked files" and the go-live claim was "0 lab
  addresses in the tree **or** the full git history". The guard enforcing it
  (`test_no_real_addresses_leaked_back_in`) only ever read `TUTORIAL.md`, so
  writing one into `checkpoint.md` — while documenting the fix for exactly this
  problem in `psoperator` — passed 327 green tests and was pushed as `e066854`.

  Scrubbed from the tree. It cannot be removed from history without a
  force-push, which the branch ruleset now forbids and which is the wrong trade
  on a published repo, so the privacy claim in `checkpoint.md` is scoped to say
  the tree is clean and the history is not.

### Added

- `TestNoLabAddressesAnywhere` — the red line is now enforced across **every
  tracked text file**, not one document. RFC1918 and RFC6598 are rejected;
  loopback and the RFC-5737 documentation ranges are allowed, because those are
  what examples should use. Reports `file:line`, and carries a self-test so a
  pattern that silently stops matching fails the build.

  The narrow guard was the actual defect. A red line enforced on one file is a
  red line that reads as enforced and is not.

## [0.7.1] — 2026-09-08

### Fixed

- **`dx run` let pxx forge an Anchored refusal.** It returned pxx's exit code
  verbatim, and `2` is `dx`'s code for "governance refused this role". pxx exits
  2 in the wild — a missing shell safeguard does it — so a caller could read
  exit 2 and conclude policy had refused the run when in fact the run was
  allowed and the work failed. Those demand opposite responses: "you are not
  permitted to run this" versus "fix your task".

  A downstream failure is now `3`, and pxx's real code moves into the message
  (`❌ pxx task failed (pxx exit 2).`) rather than being returned. Exit codes are
  named constants in `cmd_run.py`; `2` is reserved for `dx`'s own decision and
  is no longer reachable from a subprocess.

  Found by running a real `dx run` end-to-end after the 0.7.0 rename, not by
  reading the code.

### Added

- `TestExitCodeContract` in `tests/test_docs_consistency.py` — the README's
  exit-code line is now machine-checked against the constants in both
  directions (no undocumented code, no documented code the CLI cannot produce),
  plus a guard that fails the build if `cmd_run` ever returns a subprocess's
  exit code again. Each guard was verified to fire by reintroducing the defect.

## [0.7.0] — 2026-09-08

### Changed

- **Every dependency is now a public, MIT-licensed repository.** The role deck
  moved to `sdlc-agent-roles` (public, MIT — the superset that replaced the
  archived `claude-sdlc-roles`), and `dx merge` now defaults to
  `devswarm-ledger-reference`, a public reference ledger carrying synthetic rows
  and one real GPG signature. The live operational ledger stays private and is
  reached by setting `DX_LEDGER_REPO`.

  Defaults changed accordingly, which is breaking for anyone relying on them:

  | | Was | Now |
  | --- | --- | --- |
  | `DEFAULT_ROLES_PATH` | `~/ai/claude-sdlc-roles/skills/sdlc-role/roles` | `~/ai/sdlc-agent-roles/skills/sdlc-role/roles` |
  | `DEFAULT_LEDGER_REPO` | `~/ai/devswarm-ledger` | `~/ai/devswarm-ledger-reference` |

  Both remain overridable by `DX_ROLES_PATH` and `DX_LEDGER_REPO`.

### Removed

- **The `gh auth login` requirement.** `setup_dependencies.sh` no longer
  preflights the GitHub CLI or branches its clones through `gh repo clone` —
  nothing it fetches is private any more. `gh` is no longer a prerequisite
  anywhere in the install path.
- **The README's "Before you clone" section**, added one release ago in 0.6.2 to
  warn visitors that two integrations were private repos. Its reason to exist is
  gone. In its place, a "Reproducing this" section naming all five repos and how
  each is obtained.

### Fixed

- The hermetic-guard test's rationale said "private sibling repo". The repos are
  public now, so the rule was restated around what it actually protects: the
  README's claim that `pytest` runs with nothing but this repository. The rule
  itself is unchanged — a test that hard-requires a clone still needs a skip
  guard.

## [0.6.2] — 2026-09-08

### Added

- **A "Before you clone" section at the top of the README.** Two of the four
  integrations — `claude-sdlc-roles` and `devswarm-ledger` — are private repos,
  and that fact was buried in the Dependencies section. A visitor would follow
  the quick start and meet it as a 404. It is now the first thing after the
  intro, with a table of what does and does not work without them, and a
  pointer to the test suite as the surface built to be evaluated from outside.
- The README's test count is now checked against the real collected count, so
  a number a reviewer would reasonably trust cannot quietly drift.

## [0.6.1] — 2026-09-08

Go-live pass. Every claim in `TUTORIAL.md` was re-run against the real lab and
checked against actual output rather than assumed.

### Fixed

- **`dx run`'s status line printed after the output of the command it
  announces.** `print()` buffers when stdout is not a tty, while pxx writes
  straight to the inherited descriptor, so a piped or redirected run transcript
  read out of order. Same defect fixed in `dx merge` in 0.3.0; a run transcript
  is evidence too. All of `cmd_run`'s status output is now flushed.
- **`TUTORIAL.md` §6 contradicted its own instructions.** It told the reader to
  `export PXX_ALLOW_UNGATED_SHELL=1`, then showed a transcript captured without
  it — a failing run, exit 2. A reader following the steps literally gets a
  *successful* run with different output. The success path is now the primary
  transcript, with the `[HOOKS_MISSING]` failure kept as the documented
  what-if-you-skipped-it case.
- **`TUTORIAL.md` §5 showed output the command cannot produce.** The
  `dx roles list --fit High` block had column widths from before the
  hardcoded-width fix; widths are computed from the filtered data, so that
  command has never printed that table. Recaptured from a real run.
- The tutorial's `dx --version` comment still said 0.3.0, and its closing
  boundary paragraph still said the merge gate had only ever passed all-green
  against a *stubbed* signature — untrue since 0.4.0 added real-key coverage.

### Added

- `TestTutorialFidelity` in `tests/test_docs_consistency.py`. It re-runs
  `dx roles list --fit High` and fails the build if the shown transcript drifts,
  checks the version comment against the package, and asserts the address
  substitution stays declared and the boundary paragraph stays present. The
  tutorial's worth rests on "every output was captured from a real session", and
  that claim had already decayed once without anyone noticing.

## [0.6.0] — 2026-09-08

Two more fail-opens, both found by feeding odd input to commands rather than
reading them. Both are the same shape: two parts of dx disagreeing about
whether an install is usable, with the permissive one winning.

### Security

- **`dx run` executed under a role card that fails validation.** A card with a
  too-short mandate and no "Must not" section was accepted, and dx injected an
  empty `MANDATE` and an empty `MUST NOT` into the prompt and reported success.
  The governance text that is supposed to constrain the agent was silently
  blank — while `dx roles validate` had been calling that same card invalid all
  along. `dx run` now validates the card before using it and refuses, listing
  the specific failures; `--force` bypasses and says so on stderr.
- **`dx doctor` reported a healthy install with zero role cards.** No
  constitution at all passed the self-test, while `dx roles validate` correctly
  failed on the same directory.

### Changed

- **`dx run` now exits 1 for a role card that fails structural validation**,
  where it previously proceeded. A behavior change for anyone running against
  hand-edited or partial cards.
- `dx doctor` fails when the role-card directory is empty.

## [0.5.1] — 2026-09-08

### Fixed

- **`dx merge` reported a malformed approval payload as a stale signature.** Any
  payload of the form `<task_id><anything><role>` was diagnosed as "Stale
  signature (RL-003) — re-sign after re-verifying the chain", including payloads
  whose middle section was empty, non-hex, or the wrong length. Every such
  payload was correctly *rejected*; the problem was the advice. "Stale" tells an
  operator to re-sign against the current head, which is the wrong remedy for a
  tampered file and quietly launders an alteration into a fresh valid signature.

  Staleness is now claimed only when the middle section is a 64-character
  lowercase hex ledger head. Anything else reports a malformed payload, names
  the file, and says explicitly not to re-sign it. Unrecognisable payloads
  report the expected and actual byte lengths.

## [0.5.0] — 2026-09-08

Two fail-open defects in the governance layer, and the first check for the
footgun this project documents most.

### Security

- **An unparseable role card was silently dropped, and `dx roles validate` still
  reported `PASS`.** The role cards are the constitution; validate is the
  command that certifies it is intact. It printed a `WARN` to stdout, excluded
  the card, counted the rest, and exited 0 — certifying a deck with pages
  missing.

  The sharp end is Anchored roles. Those seven exist to refuse autonomous
  execution. A corrupt Anchored card vanished from the registry entirely, so
  `dx run --required_role <that role>` reported "role not found" and the
  hard-block never fired — a fail-open on the one gate whose entire job is to
  stop. Parse failures are now recorded, fail `dx roles validate`, fail
  `dx doctor`, and `dx run` distinguishes "that card is corrupt" from "no such
  role" (the latter reads like a typo and sends you looking in the wrong place).

### Added

- `config_loader.endpoint_warnings()` and a `dx doctor` warning for the
  **trailing `/v1`** mistake. It has a tutorial section, a troubleshooting row,
  and a comment in the seeded manifest — and nothing checked for it. pxx appends
  its own suffix per provider, so `http://host:8000/v1` is probed as
  `/v1/v1/models`, 404s, and every task fails with `MODEL_UNAVAILABLE`.
  Documenting a footgun is not the same as removing it. Endpoints missing a
  URL scheme are flagged too, and a test asserts the shipped seed manifest does
  not demonstrate the mistake it warns about.
- `role_registry.get_parse_failures()` and `failed_slug()`.
- `tests/test_corrupt_role_cards.py`.

### Changed

- **`dx roles validate` now exits 1 when any card fails to parse**, where it
  previously exited 0. This is the point of the release, but it is a behavior
  change for anything scripting that exit code.
- `dx doctor` parses every role card rather than counting `*.md` files, and
  reports "Role cards parse cleanly" instead of "Role cards found".

## [0.4.1] — 2026-09-08

Malformed operator input now stops the line with a message instead of a
traceback. All three defects below were found by probing the code with bad input
rather than reading it.

### Fixed

- **`dx merge` crashed on a corrupt ledger — after printing two green
  checkmarks.** A `ledger.jsonl` row that was not valid JSON raised a bare
  `JSONDecodeError` from inside the gate, past the chain and signature checks
  that had already reported success. That is the worst available shape for a
  gate failure: it looks like it is passing, then explodes. Corrupt rows now
  raise `LedgerError` naming the file and line number and invoking RL-009.
- **`dx run` printed a Python traceback for a hand-edited manifest.** Writing
  `roles:` as a list — the usual way this mistake is made — produced
  `AttributeError: 'list' object has no attribute 'get'`. The manifest is
  hand-edited by design, so a wrong shape is an ordinary operator mistake. It
  now raises `ConfigError` naming the file, the offending key, and the likely
  cause.
- **`dx doctor` reported "All core checks passed" for a manifest `dx run` could
  not use.** It validated YAML *syntax* and stopped there, so a structurally
  wrong manifest passed the self-test and failed on first use. `dx doctor` now
  loads the manifest exactly the way the commands do, and `validate_manifest()`
  checks **every** role entry rather than only the one a given command happens
  to select — validating just the default route let a malformed entry for
  another role through.
- **Unbounded subprocess calls.** `psoperator observer-health`, `audit-verify`
  and `kill` had no timeout; an emergency stop that can hang is not an emergency
  stop. `dx doctor`'s version probes and the chain verifier and gpg calls are
  bounded too. `dx run`'s call into pxx is deliberately left unbounded — that is
  the model doing the work — and carries a comment saying so.
- A `queue/<task>.json` containing a JSON array was returned as-is and failed
  later on `.get("approve_role")`, far from the cause.

### Added

- `ConfigError` for malformed manifests, and a top-level handler in `cli.main()`
  that turns both it and `LedgerError` into one line and exit 1. Programming
  errors still surface as tracebacks — only these two operator-input families
  are caught, and a test pins that distinction.
- `config_loader.validate_manifest()` — checks every section and every role.
- `tests/test_malformed_inputs.py`, including an audit that fails the build if
  any `subprocess.run` in the package lacks a timeout or a documented
  `# unbounded:` reason, plus a vacuity check so the audit cannot pass by
  matching nothing.

## [0.4.0] — 2026-09-08

Peer-review hardening. The 0.3.0 release fixed three gates that were failing
open; this one raises the floor so the next three are caught by machine rather
than by reading. Coverage 70% → 91%, `mypy --strict` clean, and the RL-003
signature gate is now proved against real GPG keys instead of captured text.

### Added

- **Real-key GPG integration tests** (`tests/test_gpg_integration.py`). Committed
  public keys and detached signatures for a usable, a revoked and an expired
  ed25519 key. The suite asserts what previously had only been reasoned about:
  `gpg --verify` **exits 0 and emits `VALIDSIG` for signatures made by revoked
  and expired keys**, so the pre-0.3.0 condition (`rc == 0` plus `VALIDSIG`)
  cannot distinguish a usable key from a retired one. It also runs the complete
  `dx merge` gate all-green against a real signature — the path that had never
  been exercised end to end.
- `tests/fixtures/gpg/regenerate.py` and a README explaining the fixtures, so
  they are reproducible rather than opaque blobs.
- **Documentation consistency tests** (`tests/test_docs_consistency.py`).
  Versions must agree across `pyproject.toml`, `dx/__init__.py` and this file;
  every environment override must be documented exactly as implemented, in both
  directions; every subcommand must appear in the README table; and no routable
  IP literal may appear in the package.
- Tests for `cmd_doctor` (11% → 82%), `psoperator_client` (24% → 100%), the
  `dx run --gui` pipeline, the VLM HTTP call, and the CLI entry point.
- `SECURITY.md` — trust boundaries, the disclosure route, and an explicit list of
  what `dx` does *not* protect against.
- `CONTRIBUTING.md` — the gates, the fixture rules, and the one standing rule:
  a gate without a test is a claim, not a gate.
- `mypy --strict` across the package, and a CI job enforcing it.
- CI now fails below 88% coverage, and fails if the GPG integration tests skip
  rather than run.

### Fixed

- **`prohibited_patterns` returned fragments, not prohibitions.** Every line of
  a Markdown bullet list was treated as its own item, so a wrapped prohibition
  became two entries — a real card's four-item "Must not" section yielded six,
  two of them sentence tails like `'argue past it.'` — while emphasis markers
  were half-consumed, leaving stray `**`. Continuation lines are now folded into
  their bullet and emphasis is stripped cleanly. This field exists to be matched
  against agent behavior, so partial sentences in it were a governance defect.
- **`cmd_doctor` hardcoded `~/ai/psoperator`** while `psoperator_client`
  honoured `PSOPERATOR_REPO`, so the two could disagree about the same install.
  Both now share `get_psoperator_repo()`, resolved per call rather than frozen
  at import.
- Six `PSOPERATOR_*` environment overrides were read but undocumented; the
  README table now matches the implementation, and a test keeps it that way.
- `pytest -q` produced no summary line, because `addopts` already supplied `-q`
  and the two combined to `-qq`.

### Changed

- All `register_*_subcommand` and `cmd_*` functions carry real argparse types
  via a shared `SubParsers` alias; `dict`/`list`/`CompletedProcess` annotations
  are parameterised.
- `cmd_doctor`'s six deferred function-local imports are hoisted to module
  scope — there was no circular dependency justifying them.
- `dev` extra now includes `pytest-cov`, `mypy` and the type stubs.

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

[0.7.2]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cdnwetzel/dx-orchestrator/releases/tag/v0.1.0
