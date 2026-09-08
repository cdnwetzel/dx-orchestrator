## What this changes

<!-- Why, not what — the diff already shows what. -->

## If this touches a gate

A gate without a test is a claim, not a gate. Three gates in 0.2.0 were failing
*open* and every one looked fine on the happy path.

- [ ] Tests cover the ways this gate can **wrongly pass**, not only the ways it
      correctly fails
- [ ] If fixing a bug: there is a test that would have caught it (or the PR says
      why not)

## Checks

- [ ] `ruff check .`
- [ ] `mypy src/dx --strict`
- [ ] `pytest` — suite still hermetic (no lab, no keyring, no network, no private repos)
- [ ] `CHANGELOG.md` updated if user-visible
- [ ] No real hostnames, addresses or usernames added to tracked files
