"""The test double's screen model: exactly what the fake command-line agents draw, read back."""

from __future__ import annotations

from tests.support.fake_screen import FakeScreen


def test_text_carriage_return_line_feed_and_backspace() -> None:
    screen = FakeScreen(20, 5)
    screen.feed(b"hello\r\nworld\rW\n")
    screen.feed(b"abc\bX")
    # A bare line feed keeps the column, as on a terminal without newline mode.
    assert screen.lines()[:3] == ["hello", "World", " abX"]


def test_clear_home_and_erase_in_line() -> None:
    screen = FakeScreen(20, 4)
    screen.feed(b"old line one\r\nold line two")
    screen.feed(b"\x1b[H\x1b[2Jnew")
    assert screen.text() == "new"
    screen.feed(b"\x1b[1;2H\x1b[K")
    assert screen.text() == "n"
    screen.feed(b"\x1b[2;1Hsecond\x1b[2;4H\x1b[1K")
    assert screen.lines()[1] == "    nd"


def test_cursor_position_and_moves() -> None:
    screen = FakeScreen(10, 3)
    screen.feed(b"\x1b[2;5Hx\x1b[Ay\x1b[2Dz")
    assert screen.lines() == ["    zy", "    x", ""]
    assert screen.cursor == (5, 0)


def test_modes_of_interest_are_tracked() -> None:
    screen = FakeScreen()
    assert screen.modes["bracketed_paste"] is False
    screen.feed(b"\x1b[?2004h")
    assert screen.modes["bracketed_paste"] is True
    screen.feed(b"\x1b[?25l\x1b[?1h")
    assert screen.modes["cursor_visible"] is False and screen.modes["app_cursor"] is True
    before = screen.mode_changes
    screen.feed(b"\x1b[?2004l")
    assert screen.modes["bracketed_paste"] is False and screen.mode_changes == before + 1


def test_alternate_screen_keeps_the_main_one() -> None:
    screen = FakeScreen(20, 3)
    screen.feed(b"shell prompt $")
    screen.feed(b"\x1b[?1049h\x1b[H\x1b[2JTUI")
    assert screen.modes["alt_screen"] is True
    assert screen.text() == "TUI"
    screen.feed(b"\x1b[?1049l")
    assert screen.text() == "shell prompt $"


def test_wrapping_and_scrolling_into_scrollback() -> None:
    screen = FakeScreen(5, 2)
    screen.feed(b"abcdefgh\r\nline3")
    assert screen.lines() == ["fgh", "line3"]
    assert screen.lines(scrollback=5) == ["abcde", "fgh", "line3"]


def test_titles_colours_and_unknown_sequences_are_consumed() -> None:
    screen = FakeScreen(30, 2)
    screen.feed(b"\x1b]0;my title\x07\x1b[1;31mred\x1b[0m \x1b]133;A\x1b\\\x1bPqdcs\x1b\\ok")
    assert screen.title == "my title"
    assert screen.text() == "red ok"


def test_utf8_split_across_feeds() -> None:
    screen = FakeScreen(20, 2)
    data = "❯ ok".encode()
    screen.feed(data[:2])
    screen.feed(data[2:])
    assert screen.text() == "❯ ok"


def test_resize_keeps_the_text() -> None:
    screen = FakeScreen(10, 3)
    screen.feed(b"one\r\ntwo\r\nthree")
    screen.resize(20, 2)
    assert screen.lines() == ["two", "three"]
    assert screen.lines(scrollback=1)[0] == "one"
