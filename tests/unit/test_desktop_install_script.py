from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "desktop" / "install.sh"


def test_install_script_is_plain_ascii():
    # The script runs under macOS /bin/sh (bash 3.2), which folds a multibyte character that
    # follows an unbraced $name into the variable name: "$repo…" became a lookup of "repo…"
    # and, under set -u, an "unbound variable" abort on every Mac.
    text = SCRIPT.read_bytes()
    bad = [i + 1 for i, line in enumerate(text.splitlines()) if any(b > 0x7F for b in line)]
    assert not bad, f"non-ASCII bytes on lines {bad}"
