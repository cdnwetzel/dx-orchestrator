"""RL-011 at the evidence sink: the bundle write is a filtered sink, not a raw pipe.

Every text artifact in every bundle family passes through :func:`redact` before
it is written (``evidence._finalize`` is the one place bundles are written, so
this is one choke point rather than one per command). A credential-shaped
string is replaced with ``[REDACTED:<label>]`` and the finding is recorded in
the bundle — the run still gets its receipt, with the secret gone and the fact
that one was there kept.

Why before the write and not after: a bundle is tamper-evident (``SHA256SUMS``)
and, once its digest is bound into the ledger, append-only in effect. You
cannot redact what you may not rewrite, so capture time is the only time.

The pattern floor is the one in ``DevSwarmX/harness/redact.py``, which guards
the ledger write. Deliberately eager: a false positive costs one unreadable
line in a diff; a false negative is a credential in a receipt for good.
Secrets only — hostnames, paths and addresses are a *publishing* concern and
belong to the pre-push scanner, not to a local receipt that legitimately
contains the operator's own paths.

Floor only, no ``gitleaks``: the receipt path must stay hermetic, bounded and
reproducible — the same run must produce the same bundle whether or not a
binary happens to be on the box. ``PATTERN_SET`` is recorded in the manifest
so a reader knows which floor applied.
"""
from __future__ import annotations

import re

#: Recorded in every manifest. Bump when PATTERNS changes, so a reader of an
#: old bundle knows which floor it passed.
PATTERN_SET = "dx-redact-v1"

# (label, pattern). Order matters only for overlapping matches; the private-key
# block goes first so its body is not partially eaten by a narrower rule.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
            r"(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
    ),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    (
        "github-token",
        re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    ),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws-secret", re.compile(r"(?i)aws.{0,20}?['\"][0-9A-Za-z/+=]{40}['\"]")),
    ("twilio-key", re.compile(r"\bSK[0-9a-f]{32}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("url-credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@")),
    (
        "secret-assignment",
        re.compile(
            # The value class excludes [ and ] so an earlier rule's
            # `[REDACTED:…]` marker is not re-matched as a fresh secret and
            # the finding keeps its real label.
            r"(?i)\b(?:api[_-]?key|secret|token|passwd|password|credential)s?\b"
            r"\s*[:=]\s*['\"][^'\"\s\[\]]{8,}['\"]"
        ),
    ),
)


def redact(text: str) -> tuple[str, list[str]]:
    """Return ``(redacted_text, findings)``.

    ``findings`` is a list like ``["openai-key×1", "jwt×2"]``; empty means the
    text came back unchanged, byte for byte. Each hit becomes
    ``[REDACTED:<label>]`` in place, so a diff stays readable around it.
    """
    findings: list[str] = []
    for label, pattern in PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{label}]", text)
        if n:
            findings.append(f"{label}×{n}")
    return text, findings


def redact_artifacts(
    artifacts: dict[str, str],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Redact every text artifact. Returns the redacted mapping and the findings
    keyed by artifact name — only names with findings appear, so an empty dict
    means nothing was touched."""
    out: dict[str, str] = {}
    found: dict[str, list[str]] = {}
    for name, content in artifacts.items():
        red, findings = redact(content)
        out[name] = red
        if findings:
            found[name] = findings
    return out, found
