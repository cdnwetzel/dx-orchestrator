#!/usr/bin/env python3
"""Live three-act staged-action runner — the first real receipted governance run.

Drives the GTK invoice fixture over live AT-SPI + Xvfb and binds the three-act
harness (:mod:`dx.staged_harness`, CI-proven) to real I/O: a real
``dx.staged_action.v1`` bundle, a real detached GPG signature, a real
hash-chained ledger, and — for act three — a real Xvfb frame move. What CI proved
with a collector and synthetic worlds, this runs against the real world; the same
seam decides.

Run on the AT-SPI host, inside a session bus with the accessibility bridge:

    DISPLAY=:99 NO_AT_BRIDGE=0 GTK_MODULES=atk-bridge \
      dbus-run-session -- .venv/bin/python examples/staged_harness_live.py --out /tmp/live

The demo signs with a throwaway software key, so every approval is honestly
recorded ``mechanism = software-ceremony (RL-010-non-compliant)`` — a genuine,
examiner-verifiable row, marked as the transitional fallback until a hardware
token lands (RL-010, Decision 0017).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path.home() / "ai" / "psoperator"))

from psoperator.fixtures.invoice import (  # noqa: E402
    DEMO_INVOICE,
    FIELD_NAMES,
    SUBMIT_NAME,
    WINDOW_TITLE,
    field_rows,
)
from psoperator.perception.a11y import AtSpiA11y

from dx import __version__ as DX_VERSION  # noqa: E402
from dx.approval_key import signing_key_residency  # noqa: E402
from dx.evidence import StagedActionBundle, bundle_digest, write_staged_action_bundle  # noqa: E402
from dx.ledger_utils import verify_detached_signature  # noqa: E402
from dx.ledger_writer import append_row, build_row, read_head  # noqa: E402
from dx.observer import frame_rgb_sha256  # noqa: E402
from dx.staged_action import WorldState, canonical_staged_message  # noqa: E402
from dx.staged_harness import (  # noqa: E402
    refuse_reproposal_without_changed_evidence,
    run_full_loop,
    run_rejection,
    run_stale_refusal,
)

ROLE = "workflow-operator"
DISPLAY = os.environ.get("DISPLAY", ":99")


def _gpg(home: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--no-tty",
         "--pinentry-mode", "loopback", "--passphrase", "", *args],
        input=stdin, capture_output=True, env={**os.environ, "GNUPGHOME": str(home)},
    )


def make_demo_key(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    r = _gpg(home, "--quick-generate-key", "Demo Operator <demo@opti3090.invalid>",
             "ed25519", "sign", "never")
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    colons = _gpg(home, "--with-colons", "--list-secret-keys").stdout.decode()
    for line in colons.splitlines():
        f = line.split(":")
        if f[0] == "fpr":
            return f[9]
    raise SystemExit("could not read demo key fingerprint")


def make_demo_ledger(repo: Path, gpg_home: Path) -> Path:
    """A fresh git ledger with a genesis row and the demo key registered."""
    keys = repo / "docs" / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    (keys / "demo.asc").write_bytes(_gpg(gpg_home, "--armor", "--export").stdout)
    genesis = build_row(action="GENESIS", task_id="STAGED-DEMO",
                        evidence="genesis for the live staged-action demo", prev_hash="0" * 64)
    ledger = repo / "ledger.jsonl"
    ledger.write_text(json.dumps(genesis, sort_keys=True, separators=(",", ":")) + "\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=demo@opti3090.invalid",
                 "-c", "user.name=Demo", "commit", "-q", "-m", "genesis"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return ledger


def capture_frame_hash() -> str:
    png = subprocess.run(["import", "-window", "root", "png:-"],
                         capture_output=True, env={**os.environ, "DISPLAY": DISPLAY}).stdout
    if not png:
        raise SystemExit("screenshot failed — is Xvfb on " + DISPLAY + "?")
    return frame_rgb_sha256(png)


def walk_fixture(prov: AtSpiA11y) -> dict:
    """Stage the fixture's fields by NAME, accepting the real AT-SPI roles
    (a GTK Entry is 'text', a Button is 'button' — psoperator #2)."""
    tree = prov.tree()
    nodes = tree.walk() if tree else []
    fields = {}
    for n in nodes:
        if n.name in FIELD_NAMES and n.role in ("text", "entry"):
            fields[n.name] = {"role": n.role, "bounds": n.bounds}
    submit = next(({"role": n.role, "bounds": n.bounds}
                   for n in nodes if n.name == SUBMIT_NAME and n.role in ("button", "push button")), None)
    return {"window": WINDOW_TITLE, "fields": fields, "submit": submit}


def stage(prov: AtSpiA11y, stage_id: str, out: Path, head: str) -> tuple[WorldState, Path, dict]:
    """Perceive + stage: walk the a11y tree, capture the frame, write a real
    dx.staged_action.v1 bundle, and bind the world-state the approval will sign."""
    located = walk_fixture(prov)
    preview = field_rows(DEMO_INVOICE)  # the operator's field-level preview
    staged_sequence = {
        "window": located["window"],
        "preview": [{"name": n, "value": v, "located": n in located["fields"]} for n, v in preview],
        "submit": located["submit"],
    }
    frame_hash = capture_frame_hash()
    payload_hash = hashlib.sha256(
        json.dumps(staged_sequence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    bundle = StagedActionBundle(
        stage_id=stage_id, title=f"Invoice entry — {DEMO_INVOICE.vendor}", state="STAGED",
        staged_sequence=staged_sequence, frame_hash=frame_hash, ledger_head=head,
        routes_to_seat=ROLE,
    )
    bundle_dir = write_staged_action_bundle(bundle, out)
    world = WorldState(ledger_head=head, frame_hash=frame_hash,
                       payload_hash=payload_hash, bundle_hash=bundle_digest(bundle_dir))
    return world, bundle_dir, located


def gpg_approval_signer(gpg_home: Path, ledger_repo: Path, world: WorldState,
                        stage_id: str, out: Path):
    """Produce and verify a real detached signature over the canonical message,
    returning the verified signer identity. This is the human/key act."""
    msg = canonical_staged_message(stage_id=stage_id, bundle_hash=world.bundle_hash,
                                   payload_hash=world.payload_hash, frame_hash=world.frame_hash,
                                   head=world.ledger_head, role=ROLE)
    msg_path = out / f"{stage_id}.msg"
    sig_path = out / f"{stage_id}.sig"
    msg_path.write_text(msg)
    r = _gpg(gpg_home, "--armor", "--detach-sign", "--output", str(sig_path), str(msg_path))
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    signer = verify_detached_signature(sig_path, msg_path, ledger_repo)  # verifies vs docs/keys/
    return signer, sig_path


def make_append(repo: Path, ledger: Path):
    def append(action: str, evidence: str) -> None:
        row = build_row(action=action, task_id="STAGED-DEMO", evidence=evidence,
                        prev_hash=read_head(ledger), reviewer_seat=ROLE)
        append_row(repo, row)
    return append


def launch_fixture(venv_python: Path) -> subprocess.Popen:
    fixture = Path.home() / "ai" / "psoperator" / "examples" / "fixtures" / "invoice_form.py"
    p = subprocess.Popen([str(venv_python), str(fixture)],
                         env={**os.environ, "DISPLAY": DISPLAY, "NO_AT_BRIDGE": "0",
                              "GTK_MODULES": "atk-bridge"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)  # let it map + register on the a11y bus
    return p


def move_window() -> None:
    """A real Xvfb frame move: shift the fixture window so the root pixels change.
    The runner does not tell act three the frame moved — it re-captures and the
    re-verification decides."""
    subprocess.run(["xdotool", "search", "--name", WINDOW_TITLE, "windowmove", "60", "60"],
                   capture_output=True, env={**os.environ, "DISPLAY": DISPLAY})
    time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live three-act staged-action runner")
    ap.add_argument("--out", required=True, help="working dir for bundles, keys, and the ledger")
    args = ap.parse_args(argv)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venv_python = Path(sys.executable)

    gpg_home = out / "gpghome"
    keyid = make_demo_key(gpg_home)
    repo = out / "demo-ledger"
    ledger = make_demo_ledger(repo, gpg_home)
    append = make_append(repo, ledger)
    print(f"demo ledger at {repo}  key {keyid[:16]}…  genesis head {read_head(ledger)[:16]}…\n")

    fixture = launch_fixture(venv_python)
    receipts = []
    try:
        prov = AtSpiA11y()  # live: pyatspi.Registry.getDesktop(0)

        # --- Act 1: the full loop ---
        world, bundle_dir, located = stage(prov, "S-ACT1", out, read_head(ledger))
        got = [n for n in FIELD_NAMES if n in located["fields"]]
        print(f"ACT 1  staged {len(got)}/{len(FIELD_NAMES)} fields {got}; frame {world.frame_hash[:12]}…")
        signer, sig = gpg_approval_signer(gpg_home, repo, world, "S-ACT1", out)
        def resolve_residency(fp: str) -> str:  # derived per-signer at the gate
            return signing_key_residency(fp, gpg_home=gpg_home)

        # Act 1's re-verification is real: re-capture the frame at execution time
        # (head/payload/bundle held fixed so the act's own appends don't self-report
        # STALE — the gate here watches the frame). Nothing moved, so it executes.
        def observe_act1() -> WorldState:
            return WorldState(
                ledger_head=world.ledger_head, frame_hash=capture_frame_hash(),
                payload_hash=world.payload_hash, bundle_hash=world.bundle_hash,
            )

        r1 = run_full_loop(stage_id="S-ACT1", role=ROLE, world=world, signer=signer,
                          resolve_residency=resolve_residency, observe_now=observe_act1,
                          executor=lambda a: print("       replay:", a["mechanism"]),
                          append=append)
        print("       ", r1.ledger_actions, "->", r1.outcome, f"({r1.detail})\n")
        receipts.append(r1.as_json())

        # --- Act 2: the receipted rejection + re-proposal gate ---
        r2 = run_rejection(stage_id="S-ACT2", reason="invoice total does not match the PO", append=append)
        try:
            # A re-proposal carrying the *same* evidence bundle as the rejected one
            # (both hashes deliberately equal) must be refused — you cannot re-ask
            # without changing what you're asking about.
            rejected_bundle = world.bundle_hash
            refuse_reproposal_without_changed_evidence(prior_bundle_hash=rejected_bundle,
                                                       new_bundle_hash=rejected_bundle)
            gate = "NOT REFUSED (bug)"
        except Exception as exc:  # noqa: BLE001
            gate = f"re-proposal refused ({type(exc).__name__})"
        print("ACT 2  ", r2.ledger_actions, "->", r2.outcome, f"; {gate}\n")
        receipts.append(r2.as_json())

        # --- Act 3: the stale refusal (real frame move) ---
        world3, _, _ = stage(prov, "S-ACT3", out, read_head(ledger))
        signer3, _ = gpg_approval_signer(gpg_home, repo, world3, "S-ACT3", out)
        print(f"ACT 3  signed against frame {world3.frame_hash[:12]}…; now moving the window…")
        move_window()

        def observe_now() -> WorldState:
            fresh = capture_frame_hash()  # the runner does not assume it moved
            return WorldState(ledger_head=world3.ledger_head, frame_hash=fresh,
                              payload_hash=world3.payload_hash, bundle_hash=world3.bundle_hash)

        r3 = run_stale_refusal(stage_id="S-ACT3", role=ROLE, world=world3, signer=signer3,
                              resolve_residency=resolve_residency, observe_now=observe_now,
                              executor=lambda a: print("       EXECUTED (should not happen)"),
                              append=append)
        print("       ", r3.ledger_actions, "->", r3.outcome, f"({r3.detail[:60]}…)\n")
        receipts.append(r3.as_json())
    finally:
        fixture.terminate()

    # Capstone: the real chain, and its provenance.
    lines = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    print(f"=== ledger: {len(lines)} rows, head {read_head(ledger)[:16]}… ===")
    for row in lines:
        print(f"  {row['action']:<10} {row['evidence'][:70]}")
    (out / "receipts.json").write_text(json.dumps(
        {"produced_by": f"dx {DX_VERSION}", "acts": receipts, "ledger_rows": len(lines)}, indent=2) + "\n")
    print(f"\nreceipts + ledger written under {out}  (produced by dx {DX_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
