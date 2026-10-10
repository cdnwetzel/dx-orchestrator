"""Failure fingerprinting for dx learn.

Two failures are "the same problem" when their gate, error class and
normalized message match. Normalization strips the volatile parts (paths,
line numbers, ids, timestamps, literal values) so a recurring defect gets one
stable signature no matter which file or value it surfaced on.
"""
from __future__ import annotations

import hashlib
import re

from .learn_model import Signature

MAX_TEXT = 300

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?\b"), "<ts>"),
    (re.compile(r"(?:[A-Za-z]:\\|\\\\)(?:[^\\\s:'\"]+\\)*[^\\\s:'\"]+"), "<path>"),
    (re.compile(r"(?<![\w<])(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+"), "<path>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    # git-style ids: 7-40 hex chars containing at least one digit and one letter
    (re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b"), "<sha>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
]

_ERROR_CLASS = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Failure|Fault))\b")
_WS = re.compile(r"\s+")


def _pick_line(detail: str) -> str:
    """The most diagnostic line: the last one naming an error class
    (Python tracebacks and pytest summaries put the real error last),
    otherwise the first non-blank line."""
    lines = [ln.strip() for ln in detail.splitlines() if ln.strip()]
    if not lines:
        return ""
    with_class = [ln for ln in lines if _ERROR_CLASS.search(ln)]
    return with_class[-1] if with_class else lines[0]


def error_class(detail: str) -> str:
    m = _ERROR_CLASS.findall(_pick_line(detail))
    return m[-1] if m else "Unclassified"


def normalize(detail: str) -> str:
    text = _pick_line(detail)
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return _WS.sub(" ", text).strip().lower()[:MAX_TEXT]


def fingerprint(gate: str, detail: str) -> Signature:
    cls = error_class(detail)
    norm = normalize(detail)
    digest = hashlib.sha256(f"{gate}\x1f{cls}\x1f{norm}".encode()).hexdigest()[:16]
    return Signature(signature_id=digest, gate=gate, error_class=cls,
                     normalized_text=norm, sample=detail.strip()[:1000])
