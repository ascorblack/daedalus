"""Pull-request text never carries session ids, trailers, private addresses or credentials."""

from daedalus.extensions.selfdev import public_references, public_text
from daedalus.security.redact import MASK


def test_public_text_strips_private_lines_addresses_and_secrets() -> None:
    raw = (
        "Fix the thing.\n\nSession: abc123\nRun: r9\nVerified on http://10.20.30.40:9000 from /home/someone/x and /srv/state/config.toml.\n"
        "Co-authored-by: bot <b@x>\nClaude-Session: https://example.invalid/s\nToken ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ok\n"
    )
    out = public_text(raw)
    assert "Session" not in out and "Co-authored" not in out and "Run:" not in out
    assert "10.20.30.40" not in out and "/home/someone" not in out and "/srv/state" not in out
    assert "ghp_" not in out and MASK in out
    assert out.startswith("Fix the thing.") and "Verified on http://<redacted>:9000" in out
    assert public_text("plain summary\n\nVerification receipts: none") == "plain summary\n\nVerification receipts: none"


def test_prose_references_to_coordination_are_named() -> None:
    body = "Requested in board thread cee13cb5 (seq 26929); review thread 16b1e0e5 round 2. Defect #7 fixed."
    found = public_references(body)
    assert "board thread" in found and "seq 26929" in found and "review thread" in found and "Defect #7" in found
    assert public_references("Split the target field so a child cannot launder a locator; sequence of 3 retries.") == []
