"""The receipt's own parsers, checked against the output the runners really print.

The counts and the tree are what the evidence gate acts on, so they are tested on their own: a parser
that silently returns "1" from a fixture would make every gate test pass while the rule did nothing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from daedalus.tools.verify import (
    UNFINGERPRINTED,
    _command_workdir,
    _content_digests,
    _counts_from_output,
    _is_test_run,
    _test_counts,
    _tree_state,
    file_digest,
)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # pytest's own footers, verbatim shapes
        ("354 passed, 16 warnings in 50.24s\n", (354, 0)),
        # the default (non-quiet) footer is padded with '=' on both sides
        ("====== 1 passed in 0.00s ======\n", (1, 0)),
        ("==== 2 failed, 30 passed in 2.85s ====\n", (32, 0)),
        ("===== 1 skipped in 0.00s =====\n", (1, 1)),
        ("===== no tests ran in 0.01s =====\n", (0, 0)),
        ("===== 1 warning in 0.01s =====\n", (0, 0)),
        ("2 failed, 30 passed, 1 skipped in 2.85s\n", (33, 1)),
        ("1 skipped in 0.00s\n", (1, 1)),
        ("1 xfailed in 0.02s\n", (1, 0)),
        ("1 xpassed in 0.00s\n", (1, 0)),
        ("3 errors in 1.20s\n", (3, 0)),
        ("27 tests collected in 0.02s\n", (0, 0)),
        ("no tests ran in 0.01s\n", (0, 0)),
        ("12 deselected in 0.01s\n", (0, 0)),
        # unittest's footer
        ("Ran 5 tests in 0.001s\n\nOK\n", (5, 0)),
        ("Ran 0 tests in 0.000s\n", (0, 0)),
        # what must NOT be read as the runner's own report
        ("the plugin said: no tests ran yesterday\n5 passed in 0.2s\n", (5, 0)),
        ("the 3 passed tokens were ignored\n", (None, None)),
        ("```\n99 passed, 2 failed in 3s\n```\n", (None, None)),  # a quoted example: no decimals on the duration
        ("", (None, None)),
        ("everything went fine\n", (None, None)),
    ],
)
def test_the_runner_footer_is_read_and_a_quotation_is_not(output: str, expected: tuple[int | None, int | None]) -> None:
    assert _test_counts(output) == expected


def test_the_last_footer_wins_and_the_window_does_not_hide_it() -> None:
    # A nested run's footer followed by the outer run's: the outer one is the run that was recorded.
    assert _test_counts("1 passed in 0.10s\nsubprocess: 7 passed in 0.20s\n9 passed in 0.30s\n") == (9, 0)
    # A footer pushed out of the tail window is still in the head, and the head is consulted as a fallback.
    assert _test_counts("5 passed in 0.10s\n" + "junk\n" * 2000) == (5, 0)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("uv run pytest tests/unit -q", True),
        ("python3 -m pytest tests -x", True),
        ("/opt/tools/bin/pytest tests", True),
        ("cd /work/checkout && uv run python -m pytest tests/unit -q", True),
        ("uv run python -m unittest discover", True),
        ("FOO=1 BAR=2 uv run pytest -q", True),
        ("timeout 600 pytest -q", True),
        ("pytest tests | tail -5", True),
        # wrappers that only pass the runner through: a package runner, a coverage driver, a shell
        ("uv run --extra dev pytest tests/unit -q", True),
        ("uvx pytest tests -q", True),
        ("uv run -m pytest tests -q", True),
        ("coverage run -m pytest tests -q", True),
        ("python -m coverage run -m pytest tests -q", True),
        ("bash -lc 'pytest tests -q'", True),
        ("nice pytest -q", True),
        ("nice -n 5 pytest -q", True),
        ("stdbuf -oL pytest -q", True),
        ("timeout 30 nice pytest -q", True),
        # mentions the runner without running it
        ("cat pytest.ini", False),
        ("grep -rn pytest log.txt", False),
        ("echo pytest", False),
        ('python -c "import pytest"', False),
        ("uv run python -c 'import pytest'", False),
        ("python -m coverage report", False),
        ("uv run ruff check pytest", False),
        # runners whose output this module does not parse are not claimed
        ("tox -e py", False),
        ("nox -s tests", False),
        ("make test", False),
        ("uv run python -m daedalus check", False),
    ],
)
def test_a_runner_is_recognised_as_an_invocation(command: str, expected: bool) -> None:
    assert _is_test_run(command) is expected


def test_a_line_printed_by_a_test_does_not_override_the_runner_footer() -> None:
    """A test can print a unittest-shaped line; the pytest footer is still the runner's own report.

    Read as one pool, the later line wins and the recorded count becomes zero — a passing run that the
    gate would then treat as evidence that nothing ran.
    """
    text = "Ran 3 tests in 0.001s\nOK\n3 passed in 0.41s\n"
    assert _test_counts(text) == (3, 0)
    # unittest really was the runner: no pytest footer anywhere, so its shape is read.
    assert _test_counts("Ran 5 tests in 0.001s\n\nOK\n") == (5, 0)


def test_the_tree_comes_from_the_directory_the_command_ran_in(tmp_path: Path) -> None:
    here = tmp_path / "session"
    worktree = tmp_path / "worktree"
    here.mkdir()
    worktree.mkdir()
    assert _command_workdir(f"cd {worktree} && uv run pytest -q", here) == worktree
    assert _command_workdir("uv run pytest -q", here) == here
    assert _command_workdir(f"cd {tmp_path / 'gone'} && pytest", here) == here  # no such directory: not claimed
    assert _command_workdir(f"cd '{worktree}' && pytest", here) == worktree


def test_the_tree_is_empty_for_a_directory_that_is_not_a_checkout(tmp_path: Path) -> None:
    assert asyncio.run(_tree_state(tmp_path)) == ""


def test_the_counts_come_from_the_tail_then_the_head() -> None:
    """A long run's footer is in the tail, a short one's in the head, and junk in the tail must not stop
    the head from being read (a non-empty junk string is truthy, so `or` would not work)."""
    assert _counts_from_output("junk\n" * 100, "5 passed in 0.10s\n") == (5, 0)
    assert _counts_from_output("9 passed in 0.30s\n", "5 passed in 0.10s\n") == (9, 0)
    assert _counts_from_output("junk\n", "still junk\n") == (None, None)


def test_the_fingerprint_is_keyed_from_the_repository_root(tmp_path: Path) -> None:
    """Reported by review: `ls-files --others` prints paths relative to the directory it ran in, while the
    digest was taken from the toplevel, so a check run from a subdirectory wrote a key that named no file
    and left the changed file uncovered. Keys are repo-relative wherever the check ran."""
    repo, _env = _repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / "new.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "kept.txt").write_text("two\n", encoding="utf-8")
    from_root = asyncio.run(_content_digests(repo))
    from_sub = asyncio.run(_content_digests(repo / "sub"))
    assert set(from_root) == {"kept.txt", "sub/new.py"}, from_root
    assert from_sub == from_root
    assert from_sub["sub/new.py"] == file_digest(repo / "sub" / "new.py")


def test_a_checkout_whose_base_cannot_be_found_says_so_rather_than_staying_silent(tmp_path: Path) -> None:
    """An empty fingerprint used to be stored when there was nothing to compare against, which the gate
    read as "no claim" and the clock then decided. The receipt now carries the reason, so a check that
    cannot name its tree is visible in the receipt and covers nothing in the gate."""
    repo, _env = _repo(tmp_path)
    digests = asyncio.run(_content_digests(repo))  # clean tree, and no origin/main to compare with
    assert list(digests) == [UNFINGERPRINTED], digests
    (repo / "kept.txt").write_text("two\n", encoding="utf-8")
    assert set(asyncio.run(_content_digests(repo))) == {"kept.txt"}


def test_the_tree_marks_a_dirty_checkout(tmp_path: Path) -> None:
    """A receipt taken with uncommitted edits says so: the commit alone is not the tree the check saw."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=repo, check=True, env=env)
    clean = asyncio.run(_tree_state(repo))
    assert clean and not clean.endswith("+worktree"), clean
    (repo / "a.txt").write_text("two\n")
    dirty = asyncio.run(_tree_state(repo))
    assert dirty.endswith("+worktree"), dirty


def _repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
    (repo / "kept.txt").write_text("one\n")
    subprocess.run(["git", "add", "kept.txt"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=repo, check=True, env=env)
    return repo, env


def test_the_fingerprint_names_the_bytes_the_check_ran_against(tmp_path: Path) -> None:
    """What the gate compares: not the whole tree - something moved is not an answer - but the files the
    checkout differs by, each with a digest of its content. A committed edit, an uncommitted one and an
    untracked file are all covered, and the digest moves exactly when the bytes do."""
    repo, _env = _repo(tmp_path)
    (repo / "kept.txt").write_text("two\n")            # uncommitted edit
    (repo / "new.txt").write_text("fresh\n")           # untracked
    digests = asyncio.run(_content_digests(repo))
    assert set(digests) == {"kept.txt", "new.txt"}, digests
    assert digests["kept.txt"] == file_digest(repo / "kept.txt")
    (repo / "kept.txt").write_text("three\n")
    assert asyncio.run(_content_digests(repo))["kept.txt"] != digests["kept.txt"]
    # Files that match the base are not in the mapping: the fingerprint is about the change, not the tree.
    assert "unchanged.txt" not in digests
    # A directory that is not a checkout makes no content claim at all - which is not a passing claim.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "x.txt").write_text("x\n")
    assert asyncio.run(_content_digests(plain)) == {}


def test_a_deleted_file_and_a_symlink_have_answers_of_their_own(tmp_path: Path) -> None:
    """Silence about a path is not a fact about it: a receipt has to be able to say "already gone" and
    "a link to these bytes" as clearly as it says a digest. A link is fingerprinted through what it
    points at, so editing the target moves the digest, and a link to nothing says which it is rather
    than reading as an ordinary file."""
    repo, env = _repo(tmp_path)
    import subprocess

    (repo / "gone.txt").write_text("bye\n")
    (repo / "target.txt").write_text("first\n")
    subprocess.run(["git", "add", "gone.txt", "target.txt"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "add gone"], cwd=repo, check=True, env=env)
    (repo / "gone.txt").unlink()                       # deleted after the branch point
    (repo / "link.txt").symlink_to("target.txt")
    (repo / "dangling.txt").symlink_to("nowhere.txt")
    digests = asyncio.run(_content_digests(repo))
    assert digests["gone.txt"] == "absent", digests
    assert digests["link.txt"] == "link:" + file_digest(repo / "target.txt"), digests
    assert digests["dangling.txt"].startswith("link-broken:"), digests
    (repo / "target.txt").write_text("second\n", encoding="utf-8")
    assert asyncio.run(_content_digests(repo))["link.txt"] != digests["link.txt"]
