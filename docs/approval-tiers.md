# Approval tiers — who may sign a candidate, decided from its diff

Charter Decision 0020 (signed by both keyholders, 2026-09-30) set the rule this
document describes. `dx merge` enforces it as the named check `approval_tier`;
`dx tier` answers it read-only.

## The three tiers

| Tier | Who may sign | Rows say |
| --- | --- | --- |
| `single-reviewer` | any registered keyholder, **including the task's author** | `approval_tier=single-reviewer`, and `sod_exception=author≠reviewer (author signed; scope single-reviewer)` when the author signed |
| `two-human` | a registered keyholder **other than the author** | `approval_tier=two-human` |
| `control-plane` | nobody — `dx run` appends `REDLINE` right after EXECUTED (the gate keeps a backstop) | `dx.run approval_tier=control-plane: …` on the REDLINE row (`dx.merge_gate …` when the gate's backstop wrote it) |

The author is the human who launched the run — PLAN.md's `author_human`, the
first row of the task. The agent that executed it is provenance on the EXECUTED
row (`run= model= pxx=`), not an author.

## How the tier is decided

1. **No declaration configured** → `two-human`. The manifest's `approval:`
   section names the executor role's declaration (psguard's `roles.json`,
   which dx reads and never writes), the role, and the `ai_root` its
   `${AI_ROOT}` scope entries resolve against.
2. **No `--repo`** → `two-human`. The chain diff cannot be read, so the exec
   surface cannot be ruled out.
3. **`--repo` is not one of the role's declared scopes** → `two-human`.
4. **The chain diff** — from the *first* task's `base_sha` in the supersede
   chain (`queue/<task>.json` `supersedes` links) to the candidate, the same
   span the review packet shows — is classified path by path:
   - a **control-plane** path (RL-008's admission list: `harness/`, `roles/`,
     `CODEOWNERS`, `red-lines.md`, `ledger*`, `queue/`, `.claude*`, anchored at
     the repo root) → `control-plane`, whatever else is true;
   - an **exec-surface** path — the files that decide what "tests passed"
     means: `conftest.py`, `pytest.ini`, `pyproject.toml`, `setup.cfg`,
     `tox.ini`, `noxfile.py`, `Makefile`, `*.mk`, `justfile`,
     `.pre-commit-config.yaml` (any depth), `.github/workflows/**` (root),
     `tests/**` (any depth), `requirements*`, `pxx.toml`, and pxx's own
     protected prefixes → `two-human`;
   - a path that cannot be classified as repo-relative (absolute, `..`) counts
     as exec surface — unclassifiable is never harmless.
5. Otherwise the **declared tier** for the scope, else the role's
   `approval_tier_default`; a scope with neither → `two-human`.

The declaration, in the executor role of `roles.json`:

```json
"pxx": {
  "scope": ["${AI_ROOT}/devswarm-pilot", "${AI_ROOT}/prod-repo"],
  "approval_tier_default": "single-reviewer",
  "approval_tier": {"${AI_ROOT}/prod-repo": "two-human"}
}
```

Every key in `approval_tier` must be one of the role's `scope` entries; a
declaration for a scope that does not exist is refused rather than ignored.

## The mechanism

Every SIGNED and MERGED row also carries `mechanism=`, derived from the
ledger's `docs/keys/REGISTRY.json` entry for the signing key — never from the
signer or a flag:

- `card` → `mechanism=hardware (RL-010: non-exportable key, gesture per signature)`
- `software` → `mechanism=fallback (RL-010 non-compliant: software key)`

The verifier imports only keys the registry lists as active. An unlisted key
file is an error; a retired key is not imported (its file stays so the rows it
signed verify by hand); the registry's `holder` must equal the key's uid name.
A ledger without a registry verifies nothing.

## What the gate refuses that it used to allow

- A task with no `author_human` — it used to warn and proceed.
- A signer whose key the registry does not know, or knows as retired.

## What `dx tier` prints

```
T-0068: single-reviewer
  - scope is single-reviewer (the default); the chain diff (3 path(s)) touches no exec-surface or control-plane path
  author: Chris Wetzel
  any registered keyholder, including the author Chris Wetzel; an author's own signature is recorded as an sod_exception
```

`--json` gives the same as an object (`tier`, `reasons`, `touched`, `changed`,
`sod_exception`, `signable`, `who_may_sign`, `chain`, `root_base`, `author`).
