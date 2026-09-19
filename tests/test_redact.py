"""`dx.redact` — the RL-011 floor at the evidence sink.

Each pattern is pinned to a fixture it must catch and a near-miss it must
leave alone, so a later "tidy-up" of a regex cannot silently blind it — the
suite, not a reviewer's eye, is what notices.
"""
import pytest

from dx.redact import PATTERN_SET, PATTERNS, redact, redact_artifacts

# (label, must-catch, must-ignore). Fixtures are synthetic: shaped like the
# real thing, never a real credential.
CASES = [
    (
        "pem-private-key",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        "-----BEGIN CERTIFICATE-----\nMIIC\n-----END CERTIFICATE-----",
    ),
    ("anthropic-key", "sk-ant-api03-" + "a" * 24, "sk-ant-"),
    ("openai-key", "sk-" + "A1b2" * 8, "sk-short"),
    ("github-token", "ghp_" + "x" * 36, "ghp_tooshort"),
    ("slack-token", "xoxb-1234567890-abcdef", "xox-nothing"),
    ("aws-access-key", "AKIAIOSFODNN7EXAMPLE", "AKIA_not_16_chars"),
    ("aws-secret", 'aws_secret = "' + "w" * 40 + '"', 'aws_region = "us-east-1"'),
    ("twilio-key", "SK" + "0" * 32, "SKIP this line"),
    ("google-api-key", "AIza" + "B" * 35, "AIzaShort"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fw", "eyJ.single"),
    ("bearer-token", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "Bearer short"),
    ("url-credentials", "postgres://user:hunter2@db.invalid/app", "https://db.invalid/app"),
    ("secret-assignment", 'api_key = "supersecretvalue"', 'api_key = ""'),
]


@pytest.mark.parametrize("label, hit, miss", CASES, ids=[c[0] for c in CASES])
def test_each_pattern_catches_its_fixture_and_ignores_the_near_miss(label, hit, miss):
    red, findings = redact(f"before {hit} after")
    assert any(f.startswith(f"{label}×") for f in findings), findings
    assert hit not in red
    assert f"[REDACTED:{label}]" in red
    assert "before " in red and " after" in red, "context around the secret must survive"

    clean, none = redact(f"before {miss} after")
    assert none == [] and clean == f"before {miss} after"


def test_every_pattern_in_the_floor_has_a_case():
    """A pattern with no fixture is a pattern nobody would notice breaking."""
    assert {label for label, _ in PATTERNS} == {c[0] for c in CASES}


def test_clean_text_is_returned_byte_identical():
    text = "diff --git a/x b/x\n+print('hello')\n+token_count = 12\n+127.0.0.1\n"
    assert redact(text) == (text, [])


def test_findings_count_repeats():
    text = "one sk-" + "q" * 20 + " two sk-" + "r" * 20
    red, findings = redact(text)
    assert findings == ["openai-key×2"]
    assert red.count("[REDACTED:openai-key]") == 2


def test_redact_artifacts_names_only_the_touched_ones():
    arts = {"clean.txt": "nothing here", "leaky.patch": "+KEY = ghp_" + "z" * 36}
    out, found = redact_artifacts(arts)
    assert out["clean.txt"] == "nothing here"
    assert "ghp_" not in out["leaky.patch"]
    assert found == {"leaky.patch": ["github-token×1"]}


def test_pattern_set_is_versioned():
    assert PATTERN_SET.startswith("dx-redact-v")
