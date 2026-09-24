"""A small terminal screen for the test double of the terminal daemon.

The daemon's real emulator answers ``read_screen`` from a full VT model. The fake command-line
agents draw line-oriented screens — clear, home, a few lines, a composer — so a model that knows
carriage return, line feed, backspace, tab, cursor position, erase in display and in line, the
bracketed-paste and alternate-screen modes and a window title is enough to read them back exactly.
Everything else it is fed (colours, other private modes, OSC it does not know) is consumed without
effect, so a real program run in the test double still leaves a readable screen, if a rougher one.

Every code point is one cell wide: the fakes draw nothing wider, and pretending otherwise would make
this a second emulator to keep correct.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field

SCROLLBACK_LINES = 2000
"""Lines kept above the main screen. The alternate screen has none, as in a real terminal."""

_MODES = {2004: "bracketed_paste", 1049: "alt_screen", 1047: "alt_screen", 47: "alt_screen", 25: "cursor_visible", 1: "app_cursor"}


@dataclass
class _Grid:
    cols: int
    rows: int
    lines: list[list[str]] = field(default_factory=list)
    x: int = 0
    y: int = 0

    def __post_init__(self) -> None:
        if not self.lines:
            self.lines = [self._blank() for _ in range(self.rows)]

    def _blank(self) -> list[str]:
        return [" "] * self.cols


class FakeScreen:
    """Feed it bytes; read back ``text()``, ``lines()``, ``cursor`` and ``modes``."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.cols = cols
        self.rows = rows
        self.main = _Grid(cols, rows)
        self.alt = _Grid(cols, rows)
        self.grid = self.main
        self.scrollback: list[str] = []
        self.modes: dict[str, bool] = {"bracketed_paste": False, "alt_screen": False, "cursor_visible": True, "app_cursor": False}
        self.title = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "ground"
        self._sequence = ""
        self._pending_wrap = False
        self._saved: tuple[int, int] = (0, 0)
        self.mode_changes = 0
        """Bumped whenever a mode of interest changes, so the daemon can publish ``terminal.mode``."""

    # -- reading ----------------------------------------------------------------------------

    def lines(self, *, scrollback: int = 0) -> list[str]:
        """The visible rows with trailing blanks trimmed, preceded by up to ``scrollback`` older lines
        (main screen only)."""
        rows = ["".join(row).rstrip() for row in self.grid.lines]
        if scrollback and self.grid is self.main:
            rows = self.scrollback[-scrollback:] + rows
        return rows

    def text(self, *, scrollback: int = 0) -> str:
        """The screen as text, without the empty rows below the last drawn one."""
        rows = self.lines(scrollback=scrollback)
        while rows and not rows[-1]:
            rows.pop()
        return "\n".join(rows)

    @property
    def cursor(self) -> tuple[int, int]:
        return self.grid.x, self.grid.y

    # -- writing ----------------------------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        for grid in (self.main, self.alt):
            for line in grid.lines:
                del line[cols:]
                line.extend([" "] * (cols - len(line)))
            while len(grid.lines) > rows:
                dropped = grid.lines.pop(0)
                if grid is self.main:
                    self._keep("".join(dropped).rstrip())
                grid.y = max(0, grid.y - 1)
            while len(grid.lines) < rows:
                grid.lines.append([" "] * cols)
            grid.cols, grid.rows = cols, rows
            grid.x, grid.y = min(grid.x, cols - 1), min(grid.y, rows - 1)
        self.cols, self.rows = cols, rows

    def feed(self, data: bytes) -> None:
        for char in self._decoder.decode(data):
            self._char(char)

    def _char(self, char: str) -> None:
        state = self._state
        if state == "ground":
            if char == "\x1b":
                self._state, self._sequence = "escape", ""
            elif char < " " or char == "\x7f":
                self._control(char)
            else:
                self._print(char)
        elif state == "escape":
            if char == "[":
                self._state, self._sequence = "csi", ""
            elif char == "]":
                self._state, self._sequence = "osc", ""
            elif char in "PX^_":
                self._state = "string"  # DCS, SOS, PM, APC: consumed up to ST
            elif char in "()*+":
                self._state = "charset"
            else:
                self._state = "ground"
                if char == "7":
                    self._saved = (self.grid.x, self.grid.y)
                elif char == "8":
                    self.grid.x, self.grid.y = self._saved
                elif char == "c":
                    self.main, self.alt = _Grid(self.cols, self.rows), _Grid(self.cols, self.rows)
                    self.grid = self.main
                elif char == "M":
                    self.grid.y = max(0, self.grid.y - 1)
        elif state == "charset":
            self._state = "ground"
        elif state == "csi":
            if "\x40" <= char <= "\x7e":
                self._state = "ground"
                self._csi(self._sequence, char)
            elif char == "\x1b":
                self._state = "escape"  # an interrupted sequence is dropped
            elif len(self._sequence) < 64:
                self._sequence += char
        elif state in ("osc", "string"):
            if char == "\x07":
                self._end_string()
            elif char == "\x1b":
                self._state = state + "_esc"
            elif state == "osc" and len(self._sequence) < 4096:
                self._sequence += char
        elif state in ("osc_esc", "string_esc"):
            if char == "\\":
                self._end_string(state == "osc_esc")
            else:
                self._state = "ground"
                self._char(char)

    def _end_string(self, osc: bool | None = None) -> None:
        if (osc if osc is not None else self._state == "osc") and self._sequence[:2] in ("0;", "2;"):
            self.title = self._sequence[2:]
        self._state, self._sequence = "ground", ""

    def _control(self, char: str) -> None:
        grid = self.grid
        if char == "\r":
            grid.x, self._pending_wrap = 0, False
        elif char in "\n\x0b\x0c":
            self._pending_wrap = False
            self._line_feed()
        elif char == "\b":
            self._pending_wrap = False
            grid.x = max(0, grid.x - 1)
        elif char == "\t":
            grid.x = min(grid.cols - 1, (grid.x // 8 + 1) * 8)

    def _line_feed(self) -> None:
        grid = self.grid
        if grid.y < grid.rows - 1:
            grid.y += 1
            return
        top = grid.lines.pop(0)
        if grid is self.main:
            self._keep("".join(top).rstrip())
        grid.lines.append([" "] * grid.cols)

    def _keep(self, line: str) -> None:
        self.scrollback.append(line)
        if len(self.scrollback) > SCROLLBACK_LINES:
            del self.scrollback[: len(self.scrollback) - SCROLLBACK_LINES]

    def _print(self, char: str) -> None:
        grid = self.grid
        if self._pending_wrap:
            self._pending_wrap = False
            grid.x = 0
            self._line_feed()
        grid.lines[grid.y][grid.x] = char
        if grid.x == grid.cols - 1:
            self._pending_wrap = True
        else:
            grid.x += 1

    def _csi(self, body: str, final: str) -> None:
        private = body.startswith("?")
        params = [int(p) if p.isdigit() else 0 for p in body.lstrip("?>=<").split(";")] if body.lstrip("?>=<") else []
        first = params[0] if params else 0
        grid = self.grid
        self._pending_wrap = False
        if private and final in "hl":
            for number in params:
                self._mode(number, final == "h")
        elif final in "Hf":
            row = (params[0] if params and params[0] else 1) - 1
            col = (params[1] if len(params) > 1 and params[1] else 1) - 1
            grid.y, grid.x = min(max(row, 0), grid.rows - 1), min(max(col, 0), grid.cols - 1)
        elif final == "A":
            grid.y = max(0, grid.y - (first or 1))
        elif final == "B":
            grid.y = min(grid.rows - 1, grid.y + (first or 1))
        elif final == "C":
            grid.x = min(grid.cols - 1, grid.x + (first or 1))
        elif final == "D":
            grid.x = max(0, grid.x - (first or 1))
        elif final == "G":
            grid.x = min(grid.cols - 1, max(0, (first or 1) - 1))
        elif final == "J":
            self._erase_display(first)
        elif final == "K":
            line = grid.lines[grid.y]
            start, end = {0: (grid.x, grid.cols), 1: (0, grid.x + 1), 2: (0, grid.cols)}.get(first, (grid.x, grid.cols))
            line[start:end] = [" "] * (end - start)

    def _erase_display(self, how: int) -> None:
        grid = self.grid
        if how in (2, 3):
            # A full clear pushes nothing into scrollback: the fakes clear to redraw, and scrollback
            # filled with old frames would be useless to read.
            grid.lines = [[" "] * grid.cols for _ in range(grid.rows)]
            if how == 3 and grid is self.main:
                self.scrollback.clear()
        elif how == 0:
            grid.lines[grid.y][grid.x :] = [" "] * (grid.cols - grid.x)
            for row in range(grid.y + 1, grid.rows):
                grid.lines[row] = [" "] * grid.cols
        elif how == 1:
            for row in range(grid.y):
                grid.lines[row] = [" "] * grid.cols
            grid.lines[grid.y][: grid.x + 1] = [" "] * (grid.x + 1)

    def _mode(self, number: int, on: bool) -> None:
        name = _MODES.get(number)
        if name is None:
            return
        if name == "alt_screen":
            if on and self.grid is self.main:
                self._saved = (self.main.x, self.main.y)
                self.alt = _Grid(self.cols, self.rows)
                self.grid = self.alt
            elif not on and self.grid is self.alt:
                self.grid = self.main
                self.main.x, self.main.y = self._saved
        if self.modes.get(name) != on:
            self.modes[name] = on
            self.mode_changes += 1


__all__ = ["SCROLLBACK_LINES", "FakeScreen"]
