# Fleet coordination channel

A git-based relay so a coordination message becomes **"go" → pull → read**, with no
copy-paste. Two nodes run the same codebase on different fleets:

- **ai-workstation** (work fleet) — the repo **writer/merger**.
- **SP9** (home fleet) — repo **read-only** (contributes via its own branches; never lands to `main` directly).

## How it works

This branch — `coord/fleet` — **never merges to `main`.** It is a comms channel, not code.
Each node writes **only its own file** and reads the other's, so the two never
write-conflict:

- `coordination/from-ai-workstation.md` — written by ai-workstation, read by SP9.
- `coordination/from-sp9.md` — written by SP9, read by ai-workstation.

A relay is now:

1. The author appends an entry to **its own** file (newest first) and pushes `coord/fleet`.
2. The other node: `git fetch origin && git checkout coord/fleet && git pull` (or `git pull` if already on it), reads the other's file, acts.
3. Acknowledgement is just **"go"** — understood, pulled, read.

Keep `main` and feature branches as they are; this branch is orthogonal to both.
`git fetch origin coord/fleet:coord/fleet` updates it without checking it out.

## Rules (non-negotiable)

- **Address-free.** This repo is public and the red line forbids fleet addresses in
  tracked files. Refer to nodes by **tier/role** (HEAVY node, SHELF node, FAST node,
  the audit proxy, psrouter, labrouter), models by **name** (`qwen3.8-27b`,
  `q36-moe`, `gpt-oss:20b`), and **ports** as contract (`:8003`, `:8007`, `:8888`).
  **Never** write octets or hostnames. Real addresses live only in each box's
  untracked `~/.config/dx/fleet_binding.yml`.
- **Append newest-first**, under your file's log heading. Date each entry
  (`YYYY-MM-DD`), give it a one-line subject, keep the body terse.
- **Write only your own file.** Reading the other's is how you receive; you never edit it.
- **Decisions that change code still go through a PR to `main`** (writer merges). This
  channel carries intent, status, and asks — not merges.
