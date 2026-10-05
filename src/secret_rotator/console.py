"""The terminal of `run <path>`: lines out, answers in. A hidden entry is read with echo and line
editing off, so it is never shown, and a long or multi-line paste arrives whole: the terminal's
own line editing drops what a line holds past its 4095th character."""

import codecs
import os
import re
import shutil
import sys
import termios
from typing import TextIO

# Bracketed paste (xterm, tmux, VS Code): while it is on, the terminal wraps a paste in these.
PASTE_ON, PASTE_OFF = "\x1b[?2004h", "\x1b[?2004l"
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CSI_BEGUN = re.compile(r"\x1b(\[[0-?]*[ -/]*)?")
ERASE = "\r\x1b[2K"


class HiddenEntry:
    """A hidden entry from what the terminal sends with echo and line editing off: Enter ends it,
    backspace takes back a character, ^U clears it, ^D on an empty entry ends the input, other keys
    that send escape sequences do nothing, and a bracketed paste is taken whole, its line breaks
    included. Trailing line breaks are dropped."""

    def __init__(self):
        self.chars: list[str] = []
        self.pasting = False
        self.after_cr = False  # in a paste: a CR came last, so a LF after it is the same break
        self.begun = ""  # an escape sequence still to complete

    def feed(self, text: str) -> str | None:
        """The entry once Enter has ended it, else None."""
        text, self.begun = self.begun + text, ""
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\x1b":
                if match := CSI.match(text, i):
                    marks = {PASTE_START: True, PASTE_END: False}
                    self.pasting = marks.get(match[0], self.pasting)
                    i = match.end()
                elif CSI_BEGUN.fullmatch(text, i):
                    self.begun = text[i:]
                    return None
                else:
                    i += 1
                continue
            i += 1
            if self.pasting:
                if not (c == "\n" and self.after_cr):
                    if c in "\r\n":
                        self.chars.append("\n")
                    elif c.isprintable() or c == "\t":
                        self.chars.append(c)
                self.after_cr = c == "\r"
            elif c in "\r\n":
                return "".join(self.chars).rstrip("\n")
            elif c in "\x7f\x08":
                if self.chars:
                    self.chars.pop()
            elif c == "\x15":
                self.chars.clear()
            elif c == "\x04" and not self.chars:
                raise EOFError
            elif c.isprintable():
                self.chars.append(c)
        return None


class Console:
    def __init__(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout):
        self.stdin = stdin
        self.stdout = stdout
        self.tty = stdin.isatty() and stdout.isatty()

    def write(self, text: str) -> None:
        self.stdout.write(text)
        self.stdout.flush()

    def _start(self) -> str:
        return ERASE if self.tty else ""

    def line(self, text: str = "") -> None:
        """A finished line, written over a live one."""
        self.write(self._start() + text + "\n")

    def live(self, text: str) -> None:
        """A line rewritten in place until the next one; none where the output is no tty."""
        if self.tty:
            self.write(ERASE + text[: shutil.get_terminal_size().columns - 1])

    def ask(self, question: str) -> str:
        """An answer, typed and shown; EOFError at the end of the input."""
        self.write(self._start() + question)
        answer = self.stdin.readline()
        if not answer:
            raise EOFError
        return answer.strip()

    def hidden(self, prompt: str) -> str:
        """An entry that is never shown; EOFError at the end of the input."""
        self.write(self._start() + prompt)
        if not self.tty:
            entry = self.stdin.readline()
            if not entry:
                raise EOFError
            return entry.rstrip("\r\n")
        fd = self.stdin.fileno()
        saved = termios.tcgetattr(fd)
        mode = termios.tcgetattr(fd)
        mode[0] &= ~termios.ICRNL
        mode[3] &= ~(termios.ECHO | termios.ICANON)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        decode = codecs.getincrementaldecoder("utf-8")(errors="replace").decode
        entry = HiddenEntry()
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        try:
            self.write(PASTE_ON)
            while True:
                data = os.read(fd, 4096)
                if not data:
                    raise EOFError
                if (value := entry.feed(decode(data))) is not None:
                    return value
        finally:
            self.write(PASTE_OFF + "\n")
            termios.tcsetattr(fd, termios.TCSANOW, saved)

    def reveal(self, value: str) -> None:
        """Shows the value until Enter, then takes it off the screen again."""
        self.write(value + "\n")
        self.ask("⏎ hides it again ")
        if self.tty:
            width = shutil.get_terminal_size().columns
            rows = sum(max(1, -(-len(line) // width)) for line in value.split("\n")) + 1
            self.write(f"\x1b[{rows}F\x1b[J")
