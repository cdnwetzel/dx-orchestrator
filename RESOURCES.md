# Resource requirements

What `dx` needs to run, and what the fleet it dispatches to needs to be useful.
`dx` itself is deliberately cheap; the expensive requirements all live on the
other end of the network.

## 1. The control plane (this repo)

`dx` is a CLI that parses Markdown, reads YAML, shells out to `pxx`/`gpg`/`ssh`,
and opens short-lived TCP connections. It runs no model and holds no state.

| Resource | Requirement | Notes |
| --- | --- | --- |
| CPU | Any x86-64 or arm64 | No inference happens here |
| RAM | ~100 MB resident | Peak is the role registry (40 cards ≈ 420 KB of text) |
| Disk — source | ~200 KB | 21 tracked files |
| Disk — venv | ~130 MB | Dominated by `pxx-orchestrator` and its deps |
| Disk — sibling clones | ~30 MB | `psoperator`, `sdlc-agent-roles`, `devswarm-ledger-reference` |
| Python | 3.11+ | 3.13 tested in CI |
| Network | Outbound TCP to each configured endpoint | No inbound ports |
| GPU | **None** | By design — Phase 1 runs on a 2011 Mac Mini or a Surface Pro 6 |

Validated driver hardware: Surface Pro 6 (i5-8250U, 8 GB) running Ubuntu 24.04
under WSL2. A `dx roles list` completes in well under a second on that box; the
only slow command is `dx run`, and all of that time is remote inference.

## 2. Required host tooling

| Tool | Why | Checked by |
| --- | --- | --- |
| `python3` ≥ 3.11 | Runtime floor | `dx doctor` |
| `virtualenv` or `python3-venv` | Ubuntu 24.04+ refuses bare `pip install` (PEP 668) | — |
| `git` | Clones, and `pxx` writes `pxx-pre/` safety tags | — |
| `gpg` | RL-003 signature verification in `dx merge` | `dx doctor` |
| `ssh` | Screenshot capture for `dx verify-gui` | — |
| ImageMagick (`import`) **on the GUI host** | Default screenshot command | — |

## 3. Inference fleet

`dx` does not care what hardware sits behind an endpoint — only that it speaks
the declared `provider` protocol. These are the requirements each *role class*
imposes, so you can size a node rather than copy someone else's lab.

| Role class | Model class | VRAM floor | Protocol | Notes |
| --- | --- | --- | --- | --- |
| Bulk generation (`backend-engineer`, `security-architect`) | 27–32B, FP8/AWQ | 40 GB (2×24 GB works) | vLLM | The throughput-bound path; ~33 tok/s observed on 2×A4500 |
| Fast iteration (`frontend-engineer`) | 14B class | 16 GB | Ollama | Latency matters more than quality here |
| GUI verification | VLM, 3–7B | 8 GB | Ollama | Must accept `images:` in `/api/generate` |
| Fallback (`default`) | Anything | — | Ollama | `http://localhost:11434` out of the box |

**Sizing rule of thumb.** `output = min(generation, verification)`. Adding
generation capacity past the point where a human can verify the output buys
nothing. Size the fleet to the review throughput you actually have, not to the
GPUs you can afford.

### Protocol requirements

- `endpoint` values **must not** carry a trailing `/v1`. `pxx` appends its own
  suffix per provider (`/api/tags` for ollama, `/v1/models` for vllm/openai);
  a doubled `/v1` yields a 404 and `MODEL_UNAVAILABLE` on every task.
- vLLM endpoints must expose `/v1/models` and an OpenAI-compatible
  `/v1/chat/completions`.
- Ollama endpoints must expose `/api/tags` and `/api/generate`.
- `dx doctor`'s probes are TCP-connect only, so a server that 404s on `/`
  still reports reachable.

## 4. Storage growth

Nothing in this repo grows without bound today, because evidence-bundle
generation is not yet wired. When it lands, plan for it:

| Artifact | Owner | Growth |
| --- | --- | --- |
| `ledger.jsonl` | `devswarm-ledger` | ~300 bytes/row, append-only, hash-chained |
| `approvals/*.asc` | `devswarm-ledger` | ~900 bytes per approval |
| Evidence bundles | `dx run` (**not yet implemented**) | Directory per run; screenshots dominate |
| `psoperator_audit.jsonl` | PSOperator | One row per GUI action |

Housekeeping rule for this project: **no `archive/` or `backup/` directories
inside the repo.** History lives in Git; evidence lives in the ledger repo.
`.gitignore` enforces the first half of that.

## 5. What this project deliberately does not require

- **No cloud API account.** Sovereignty is the invariant; there is no fallback
  path to a hosted model, and one must not be added.
- **No GPU on the driver box.** Phase 1 targets low-power hardware on purpose.
- **No inbound network.** `dx` never listens on a port.
- **No always-on daemon.** Every command is a short-lived process.
- **No database.** The ledger is a Git repo; the config is a YAML file.
