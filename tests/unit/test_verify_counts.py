"""The receipt's own parsers, checked against the output the runners really print.

The counts and the tree are what the evidence gate acts on, so they are tested on their own: a parser
that silently returns "1" from a fixture would make every gate test pass while the rule did nothing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from daedalus.tools.verify import _command_workdir, _is_test_run, _test_counts, _tree_state


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
        ("/srv/venv/bin/pytest tests", True),
        ("cd /srv/state/worktrees/bot/x && uv run python -m pytest tests/unit -q", True),
        ("uv run python -m unittest discover", True),
        ("FOO=1 BAR=2 uv run pytest -q", True),
        ("timeout 600 pytest -q", True),
        ("pytest tests | tail -5", True),
        # mentions the runner without running it
        ("cat pytest.ini", False),
        ("grep -rn pytest log.txt", False),
        ("echo pytest", False),
        ('python -c "import pytest"', False),
        ("uv run python -c 'import pytest'", False),
        # runners whose output this module does not parse are not claimed
        ("tox -e py", False),
        ("nox -s tests", False),
        ("make test", False),
        ("uv run python -m daedalus check", False),
    ],
)
def test_a_runner_is_recognised_as_an_invocation(command: str, expected: bool) -> None:
    assert _is_test_run(command) is expected


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
