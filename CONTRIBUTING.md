# Contributing

## Setup

```bash
python3 -m venv .venv || virtualenv -p python3 .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The test suite is hermetic. It needs no lab hardware, no GPG keyring, no network
and none of the sibling clones — if a change makes that untrue, the change
is wrong. `gpg` on `PATH` unlocks the integration tests; without it they skip.

## The gates

CI runs all of these; run them before pushing.

```bash
ruff check .                       # lint + import order
mypy src/dx --strict               # no untyped defs, no implicit Any
pytest                             # the full suite
pytest --cov=dx --cov-report=term  # coverage must not fall below the CI floor
```

## The one rule that matters

**A gate without a test is a claim, not a gate.**

This project exists to enforce invariants mechanically. An invariant enforced by
code that is never exercised against its own failure modes is no better than a
prompt asking nicely. Three gates in 0.2.0 were failing *open* — the
separation-of-duties check could never match, revoked keys were accepted, and an
unconfigured install silently used a hardcoded remote host. Every one was
invisible on the happy path, and invisible on the negative path that had been
demonstrated by hand.

So: when you add or change a gate, add tests for **the ways it can wrongly
pass**, not just the ways it correctly fails. If you cannot write a test that
would have caught the bug you are fixing, say so in the PR and explain why.

Two corollaries, both learned the hard way:

- **Design the failure output, not just the success path.** `dx merge` once
  printed two green checkmarks and then a traceback on a corrupt ledger. Errors
  must name the file, the line where possible, and what to do next.
- **Bound every shell-out.** A hung gate gets bypassed. `subprocess.run` without
  a `timeout=` fails the build unless it carries a `# unbounded:` comment saying
  why — `dx run` waits on model generation and legitimately has no deadline.

## Test conventions

- **Fixtures are synthetic.** Role cards in `tests/fixtures/roles/` mirror the
  structure of the `sdlc-agent-roles` cards with invented prose. Never
  vendor real card text into this repo.
- **No real hosts anywhere.** Test endpoints use `.invalid` (RFC 6761 guarantees
  it never resolves). A hardcoded routable address in the package fails
  `tests/test_docs_consistency.py`.

  This is the **fleet-wide addressing policy**, and it holds identically in
  `psoperator` (see its `CONTRIBUTING.md` §4): example configuration uses
  reserved documentation ranges — RFC 6761 `.invalid` names here, RFC 5737
  `192.0.2.0/24` and `203.0.113.0/24` in `psoperator`'s fleet presets. There is
  **no approved exception**: a real deployment address or hostname in a tracked
  file is a defect whatever range it comes from, RFC1918 included. `psoperator`
  carried such an exception until 2026-09-08 and no longer does.
- **No wall-clock dependencies.** The GPG fixtures are committed rather than
  generated because the generated version was flaky on a host whose clock
  stepped backwards. See `tests/fixtures/gpg/README.md`.
- **Guard anything needing a sibling clone** with `pytest.mark.skipif`, as
  `TestAgainstRealCards` does.
- Name the behavior, not the function: `test_revoked_key_is_rejected`, not
  `test_verify_2`.

## Documentation is checked

`tests/test_docs_consistency.py` verifies that versions agree across
`pyproject.toml`, `dx/__init__.py` and `CHANGELOG.md`; that every environment
override is documented exactly as implemented; and that every CLI subcommand
appears in the README table. Add an override without documenting it and the
suite fails. That is deliberate.

Update `CHANGELOG.md` in the same commit as a user-visible change.

## What not to build

From `VISION.md`, and not negotiable:

- **No cloud fallback.** Sovereignty is the point; an escape hatch erodes it.
- **No LLM-driven gates** (RL-007). Every gate is regex, YAML, or GPG.
- **No harness-reachable signing keys** (RL-010).
- **No hardcoded remote hosts.** Every external path is overridable.
- **No `archive/` or `backup/` directories.** History lives in git.

## Commits and PRs

Explain *why* in the commit body — the diff already shows what. For a fix, say
what the bug let through. Small, reviewable commits; one concern each.
