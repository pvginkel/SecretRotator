"""The UI's widgets (design §7.2, §7.4): the status bar, the boxes, their state area's parts — the
step log, the fields, the shown value — the question dialog, Details and help."""

import datetime
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.markup import escape
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Static, TextArea

from secret_rotator.model import label
from secret_rotator.nightly import HORIZON
from secret_rotator.opsteps import Field, expiry_problem
from secret_rotator.ui.item import Item, Line, LineState, Phase

if TYPE_CHECKING:
    from secret_rotator.ui.app import RotatorApp

W = TypeVar("W", bound=Widget)

# textual-dark's palette, so Rich text matches the stylesheet's $accent, $text-accent, …
ACCENT = "#FEA62B"  # $accent
HEAD = "#FFC473"  # $text-accent: the app name, a due date within the first Telegram warning
OK = "#8AD4A1"  # $text-success
ERR = "#D17E92"  # $text-error
RUN = "#57A5E2"  # $text-primary
DIM = "#8a8f98"
DUE = "#C0C0C0"  # untouched, due or not
GREYED = "#777E84"  # $text-disabled on the footer's $panel: a disabled key

GLYPH = {
    Phase.DUE: "○",
    Phase.RUNNING: "●",
    Phase.WAITING: "●",
    Phase.DONE: "✓",
    Phase.IN_FLIGHT: "◐",
    Phase.FAILED: "✗",
    Phase.ROLLING_BACK: "●",
    Phase.ROLLED_BACK: "○",
    Phase.ROLLBACK_FAILED: "✗",
}
SPINNER = "◐◓◑◒"  # a running line's glyph, turning
LINE_GLYPH = {LineState.OK: ("✓", OK), LineState.FAILED: ("✗", ERR)}

# A box's border and title colour, selected or not (§7.2): untouched light grey, your move (a
# wizard waits on you, or was left in flight) orange, the tool's move blue, failed red, done green.
# Each is a class of the stylesheet's.
TONE = {
    Phase.DUE: "due",
    Phase.RUNNING: "primary",
    Phase.WAITING: "accent",
    Phase.DONE: "success",
    Phase.IN_FLIGHT: "accent",
    Phase.FAILED: "error",
    Phase.ROLLING_BACK: "primary",
    Phase.ROLLED_BACK: "due",
    Phase.ROLLBACK_FAILED: "error",
}
TONES = tuple(dict.fromkeys(TONE.values()))
TONE_COLOUR = {"due": DUE, "accent": ACCENT, "primary": RUN, "error": ERR, "success": OK}
TONE_TEXT = {"due": "#E0E0E0", "accent": HEAD, "primary": RUN, "error": ERR, "success": OK}
MAX_LINES = 8  # a step log's lines at most; a failed line's error row comes on top


# --- formatting ---------------------------------------------------------------


def minutes(seconds: int) -> str:
    """An estimate rounded to the minute, half up; `<1 min` under 30 s (§7.4)."""
    return "<1 min" if seconds < 30 else f"~{int(seconds / 60 + 0.5)} min"


def clock(seconds: float) -> str:
    """A line's elapsed time: `0.5s`, `5m0s`."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds + 0.5), 60)
    return f"{m}m{s}s"


def spinner() -> str:
    return SPINNER[int(time.monotonic() * 8) % len(SPINNER)]


def bar(fraction: float, colour: str, width: int = 18) -> Text:
    filled = max(0, min(width, round(fraction * width)))
    return Text.assemble(("━" * filled, colour), ("─" * (width - filled), DIM))


def header_colour(item: Item) -> str:
    """A header in a box: a paler version of its border's colour."""
    return TONE_TEXT[TONE[item.phase]]


def due_part(due_at: datetime.date | None, today: datetime.date) -> Text:
    """The due text in its colour (R87, R88): red while due and for no due date, pale orange within
    the first Telegram warning of falling due, plain further out."""
    if due_at is None:
        return Text("no due date configured", style=ERR)
    if due_at == datetime.date.min:
        return Text("due now", style=ERR)
    days = (due_at - today).days
    if days < 0:
        return Text(f"overdue {-days}d", style=ERR)
    if days == 0:
        return Text("due today", style=ERR)
    words = f"due in {days} day{'' if days == 1 else 's'}"
    return Text(words, style=HEAD if due_at - today <= HORIZON else "")


def since_text(when: datetime.datetime, now: datetime.datetime) -> str:
    """when, in now's zone: its time alone today, else yesterday's or its date's."""
    when = when.astimezone(now.tzinfo)
    if when.date() == now.date():
        day = ""
    elif when.date() == now.date() - datetime.timedelta(days=1):
        day = "yesterday "
    else:
        day = f"{when.day} {when:%b} "
    return f"in flight since {day}{when:%H:%M}"


def title_markup(item: Item, selected: bool) -> str:
    """Glyph · leaf#key · the credential in words, in the border's colour; the selected box's path
    in bold."""
    path = escape(item.id)
    path = f"[b]{path}[/b]" if selected else path
    return f"{GLYPH[item.phase]} {path} · {escape(item.rotation.credential)}"


def info_text(item: Item, today: datetime.date, now: datetime.datetime) -> Text:
    """The information line (§7.2, §7.3): while the wizard runs, the plan's estimate is kept."""
    r = item.rotation
    if item.phase is Phase.DONE:
        return Text("done", style=f"bold {OK}")
    if item.phase is Phase.ROLLED_BACK:
        return Text("rolled back", style="bold")
    if item.phase is Phase.IN_FLIGHT:
        return Text(f"{since_text(r.flight.since, now)} · {r.plan.ask} · {minutes(item.estimate)}")
    due = due_part(r.due_at, today)
    if item.phase is Phase.FAILED:
        where = f"step {item.screen.index + 1} of {len(item.screens)} · {item.stopped_at()}"
        return Text.assemble(due, " · ", (f"failed at {where}", ERR))
    if item.phase is Phase.ROLLBACK_FAILED:
        at = item.stopped_at()
        failed = f"rollback failed at {at}" if at else "rollback failed"
        return Text.assemble(due, " · ", (failed, ERR))
    return Text.assemble(due, f" · {r.plan.ask} · {minutes(item.estimate)}")


# --- buttons ------------------------------------------------------------------


@dataclass(frozen=True)
class ButtonSpec:
    default: bool  # the screen's default button: blue
    label: str
    action: str  # the argument of the app's press_button
    enabled: bool = True
    reason: str = ""  # shown beside it while it is disabled (§7.4)


class ActionButton(Button):
    """A compact Textual button (R42). It takes focus: Tab reaches it, ⏎ presses it. No key is
    printed on it (R52, R64)."""

    def __init__(
        self,
        label: str,
        action: str,
        *,
        variant: str = "default",
        disabled: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__(
            label,
            variant=variant,  # type: ignore[arg-type]
            action=action,
            compact=True,
            disabled=disabled,
            id=id,
        )

    @staticmethod
    def variant_of(spec: ButtonSpec) -> str:
        return "primary" if spec.default else "error" if spec.action == "abort" else "default"

    @classmethod
    def of(cls, spec: ButtonSpec) -> "ActionButton":
        return cls(
            spec.label,
            f"app.press_button('{spec.action}')",
            variant=cls.variant_of(spec),
            disabled=not spec.enabled,
        )

    def update_from(self, spec: ButtonSpec) -> None:
        self.label = spec.label
        self.variant = self.variant_of(spec)  # type: ignore[assignment]
        self.disabled = not spec.enabled

    @staticmethod
    def width_of(spec: ButtonSpec) -> int:
        """The cells it takes in a bar: Textual's 16-cell minimum, its label padded by one, the
        2-cell gap."""
        return max(16, Text(spec.label).cell_len + 2) + 2


class ButtonBar(Horizontal):
    """The last line of the state area: the buttons left, the progress right (§7.4)."""

    DEFAULT_CSS = """
    ButtonBar { height: 1; margin-top: 1; }
    ButtonBar > ActionButton { margin-right: 2; }
    ButtonBar > .reason {
        width: 1fr; color: $text-muted; text-style: italic; text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ButtonBar > .progress { width: auto; margin-left: 2; }
    """

    BINDINGS = [Binding("left", "step(-1)", show=False), Binding("right", "step(1)", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self.specs: tuple[ButtonSpec, ...] = ()
        self.progress = Text()
        self.compact = Text()
        # The buttons it shows: none of those it showed before, which are still children, mounted
        # and focusable while Textual removes them.
        self.buttons: list[ActionButton] = []

    def action_step(self, delta: int) -> None:
        """← → move between the buttons."""
        buttons = [b for b in self.buttons if b.focusable]
        if buttons and self.app.focused in buttons:
            buttons[(buttons.index(self.app.focused) + delta) % len(buttons)].focus()

    def compose(self) -> ComposeResult:
        yield Static(classes="reason")
        yield Static(classes="progress")

    def on_mount(self) -> None:
        self._apply()

    def on_resize(self) -> None:
        self._apply()

    def set(self, specs: Sequence[ButtonSpec], progress: Text, compact: Text | None = None) -> None:
        self.specs = tuple(specs)
        self.progress = progress
        self.compact = compact if compact is not None else progress
        self._apply()

    def ordered(self) -> list[ButtonSpec]:
        """A disabled button with a reason goes last, its reason beside it (§7.4)."""
        plain = [s for s in self.specs if s.enabled or not s.reason]
        reasoned = [s for s in self.specs if not s.enabled and s.reason]
        return [*plain, *reasoned]

    def reason(self) -> str:
        return " · ".join(dict.fromkeys(s.reason for s in self.specs if not s.enabled and s.reason))

    def _apply(self) -> None:
        try:
            reason = self.query_one(".reason", Static)
            progress = self.query_one(".progress", Static)
        except NoMatches:  # not composed yet: on_mount applies it
            return
        ordered = self.ordered()
        buttons = self.buttons
        if [b.action for b in buttons] == [f"app.press_button('{s.action}')" for s in ordered]:
            for button, spec in zip(buttons, ordered, strict=True):  # a focused one keeps focus
                button.update_from(spec)
        else:
            with self.app.batch_update():
                for button in buttons:
                    button.remove()
                self.buttons = [ActionButton.of(s) for s in ordered]
                self.mount_all(self.buttons, before=reason)
        reason.update(Text(self.reason()))
        # Short of room it drops the progress bar; the reason then shortens with an ellipsis.
        used = sum(ActionButton.width_of(s) for s in self.specs) + len(self.reason()) + 2
        fits = not self.size.width or used + self.progress.cell_len <= self.size.width
        progress.update(self.progress if fits else self.compact)


# --- the state area's parts ---------------------------------------------------


class Description(Static):
    DEFAULT_CSS = "Description { height: auto; }"


class Instruction(Static):
    """An operator step's title in bold, in the box's header colour, its instruction under it."""

    DEFAULT_CSS = "Instruction { height: auto; }"

    @staticmethod
    def build(title: str, instruction: str, colour: str) -> Text:
        out = Text(title, style=f"bold {colour}")
        for line in instruction.splitlines():
            out.append("\n")
            out.append(line)
        return out


class Notice(Static):
    """What the session said of the box's last run: a lock held, an error."""

    DEFAULT_CSS = "Notice { height: auto; margin-top: 1; color: $text-muted; }"


class Spacer(Static):
    """The blank line between an operator screen's parts and its step log."""

    DEFAULT_CSS = "Spacer { height: 1; }"


class StepLog(Widget):
    """The live step log (§7.4): a line per step that started, glyph, words and elapsed time."""

    DEFAULT_CSS = "StepLog { height: auto; }"

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[Line] = []

    def set_lines(self, lines: Sequence[Line]) -> None:
        self.lines = list(lines)
        self.refresh(layout=True)

    def get_content_height(self, container, viewport, width: int) -> int:
        return max(1, len(self.rows(width)))

    def render(self) -> Text:
        return Text("\n").join(self.rows(self.size.width))

    def rows(self, width: int) -> list[Text]:
        """At most MAX_LINES lines: the earliest finished ones fold into the first. A line appears
        when its step starts and a failure stops the run, so the folded lines are all done."""
        lines, rows = self.lines, []
        if len(lines) > MAX_LINES:
            cut = len(lines) - MAX_LINES + 1
            folded, lines = lines[:cut], lines[cut:]
            body = Text(f"{len(folded)} earlier steps", style=DIM)
            elapsed = Text(clock(sum(line.spent() for line in folded)), style=DIM)
            rows.append(self._row(Text("✓ ", style=OK), body, elapsed, width))
        for line in lines:
            failed, running = line.state is LineState.FAILED, line.state is LineState.RUNNING
            symbol, colour = (spinner(), RUN) if running else LINE_GLYPH[line.state]
            glyph = Text(f"{symbol} ", style=colour)
            body = Text(label(line.step, line.action), style=ERR if failed else "bold" * running)
            if line.detail:
                body.append(" · ")
                body.append(line.detail, style=ERR if failed else DIM)
            if line.attempt > 1:
                body.append(f" · attempt {line.attempt}", style=HEAD)
            rows.append(self._row(glyph, body, Text(clock(line.spent()), style=DIM), width))
            if failed and line.error:
                rows.append(Text.assemble("  ", (line.error, ERR)))
        return rows

    @staticmethod
    def _row(glyph: Text, body: Text, elapsed: Text, width: int) -> Text:
        room = max(10, width - glyph.cell_len - elapsed.cell_len - 2)
        body.truncate(room, overflow="ellipsis")
        pad = max(2, width - glyph.cell_len - body.cell_len - elapsed.cell_len)
        return Text.assemble(glyph, body, " " * pad, elapsed)


def masked(value: str, revealed: bool) -> Text:
    """A shown value: dots, or the value itself when revealed."""
    lines = value.rstrip("\n").split("\n")
    if not revealed:
        dots = Text("•" * min(48, len(value.strip())))
        if len(lines) > 1:
            dots.append(f"   {len(lines)} lines hidden", style=DIM)
        return dots
    shown = lines[:14]
    out = Text("\n".join(shown))
    if len(lines) > len(shown):
        out.append(f"\n… {len(lines) - len(shown)} more lines", style=DIM)
    return out


class SecretTextArea(TextArea):
    """A multi-line field: a TextArea whose characters are drawn as dots while masked, so its line
    lengths show."""

    def __init__(self, text: str, *, masked: bool) -> None:
        super().__init__(
            text,
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            placeholder="paste or type the whole file",
        )
        self.masked = masked

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        return Text("•" * len(line.plain), end="", no_wrap=True) if self.masked else line


class CredentialField(Vertical):
    """One field of a credential screen: a normal input, masked until Reveal, in a rounded box
    titled with its key, and its check on the line under it: its size and how it meets its
    shape. drafts holds what is entered, by key, while the screen is up."""

    DEFAULT_CSS = """
    CredentialField { height: auto; margin-top: 1; }
    CredentialField > .field-status { padding-left: 2; }
    """

    class Changed(Message):
        pass

    def __init__(self, field: Field, drafts: dict[str, str], revealed: bool) -> None:
        super().__init__()
        self.field = field
        self.drafts = drafts
        self.revealed = revealed

    @property
    def multiline(self) -> bool:
        return self.field.shape is not None and self.field.shape.multiline

    def compose(self) -> ComposeResult:
        editor: Widget
        if self.multiline:
            editor = SecretTextArea(self.value, masked=not self.revealed)
        else:
            editor = Input(
                self.value,
                password=not self.revealed,
                placeholder="paste or type",
                select_on_focus=False,
            )
        editor.border_title = self.field.key
        editor.add_class("editor")
        yield editor
        yield Static(self.status(), classes="field-status")

    @property
    def editor(self) -> Input | SecretTextArea:
        return self.query_one(".editor")  # type: ignore[return-value]

    @property
    def value(self) -> str:
        return self.drafts.get(self.field.key, "")

    def set_value(self, value: str) -> None:
        editor = self.editor
        if isinstance(editor, SecretTextArea):
            editor.load_text(value)
        else:
            editor.value = value
        self._store(value)

    def set_revealed(self, revealed: bool) -> None:
        if revealed == self.revealed:
            return
        self.revealed = revealed
        editor = self.editor
        if isinstance(editor, SecretTextArea):
            editor.masked = not revealed
            editor.refresh()
        else:
            editor.password = not revealed

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._store(event.value)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        event.stop()
        self._store(event.text_area.text)

    def _store(self, value: str) -> None:
        changed = self.value != value
        self.drafts[self.field.key] = value
        self.query_one(".field-status", Static).update(self.status())
        if changed:
            self.post_message(CredentialField.Changed())

    def mismatch(self) -> str:
        """Its shape's words when it holds a value that does not have the shape; else ''."""
        shape, value = self.field.shape, self.value
        return shape.words if shape and value.strip() and not shape.test(value) else ""

    def status(self) -> Text:
        value = self.value
        if not value.strip():
            return Text("empty", style=DIM)
        n = len(value.strip())
        size = f"{len(value.rstrip().splitlines())} lines · {n} chars" if self.multiline else ""
        size = size or f"{n} chars"
        shape = self.field.shape
        if shape is None:
            return Text(size, style=DIM)
        if shape.test(value):
            return Text.assemble((size, DIM), "  ", (f"✓ {shape.words}", OK))
        return Text.assemble((size, DIM), "  ", (f"⚠ expected: {shape.words}", HEAD))


class ExpiryField(Vertical):
    """The new credential's expiry: a plain input titled `expires`, and what it means on the line
    under it — in how many days, never, or why it is no expiry (R91). drafts holds it under key."""

    DEFAULT_CSS = """
    ExpiryField { height: auto; margin-top: 1; }
    ExpiryField > .field-status { padding-left: 2; }
    """

    def __init__(self, key: str, drafts: dict[str, str], today: datetime.date) -> None:
        super().__init__()
        self.key = key
        self.drafts = drafts
        self.today = today

    def compose(self) -> ComposeResult:
        editor = Input(self.value, placeholder="YYYY-MM-DD, blank for none", select_on_focus=False)
        editor.border_title = "expires"
        editor.add_class("editor")
        yield editor
        yield Static(self.status(), classes="field-status")

    @property
    def editor(self) -> Input:
        return self.query_one(".editor", Input)

    @property
    def value(self) -> str:
        return self.drafts.get(self.key, "")

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.drafts[self.key] = event.value
        self.query_one(".field-status", Static).update(self.status())

    def problem(self) -> str | None:
        """Why what it holds is no expiry; None for a date after today, or blank: none."""
        text = self.value.strip()
        return expiry_problem(text, self.today) if text else None

    def status(self) -> Text:
        text = self.value.strip()
        if not text:
            return Text("never expires", style=DIM)
        if problem := self.problem():
            return Text(f"⚠ {problem}", style=HEAD)
        days = (datetime.date.fromisoformat(text) - self.today).days
        return Text(f"✓ in {days} day{'' if days == 1 else 's'}", style=OK)


class CredentialFields(Vertical):
    DEFAULT_CSS = "CredentialFields { height: auto; }"


class ValueBox(Static):
    """An operator.show step's value, drawn like a credential field: a rounded box titled with its
    name, masked until Reveal."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.border_title = name

    def show(self, value: str, revealed: bool) -> None:
        self.update(masked(value, revealed))


# --- the list -----------------------------------------------------------------


class StateArea(Vertical):
    """What a selected box shows under its information line (§7.2)."""

    DEFAULT_CSS = "StateArea { height: auto; padding-top: 1; }"

    def __init__(self) -> None:
        super().__init__()
        self.signature: tuple = ()  # the parts it holds: what they show, and of which screen
        self.actions: tuple = ()  # the buttons it last showed: a change moves focus in
        self.parts: list[Widget] = []

    def of(self, kind: type[W]) -> list[W]:
        """Its parts of the kind, and theirs: none of the parts it held before, which are still
        children while Textual removes them."""
        found = [w for part in self.parts for w in (part, *part.query(kind))]
        return [w for w in found if isinstance(w, kind)]

    def buttons(self) -> list[ActionButton]:
        """The buttons its button bar shows: none of those Textual is still removing."""
        return [button for bar in self.of(ButtonBar) for button in bar.buttons]


class Box(Vertical):
    """One rotation: a full box whatever its state, its state area shown iff it is selected
    (R26)."""

    def __init__(self, item: Item) -> None:
        super().__init__()
        self.item = item
        self.revealed = False  # its screen's value shown in the clear
        self.copied_until = 0.0  # monotonic: Copy reads `✓ Copied` until then

    def compose(self) -> ComposeResult:
        yield Static(classes="info")
        yield StateArea()

    @property
    def area(self) -> StateArea:
        return self.query_one(StateArea)

    def on_click(self, event: events.Click) -> None:
        """A click selects the box and opens it, as ⏎ does; a click on a button or field keeps
        its focus."""
        app: RotatorApp = self.app  # type: ignore[assignment]
        app.select(self.item.id)
        if app.focused is None or self not in app.focused.ancestors:
            app.action_open()


class BoxList(VerticalScroll, inherit_bindings=False):
    """The list of boxes. It keeps none of a scroll container's keys: with a button focused,
    ↑ ↓ Home End would scroll the view instead of selecting (§7.6)."""


class StatusBar(Static):
    pass


class EmptyState(Static):
    pass


class FilterNotice(Static):
    """What a filtered list gone empty shows (§7.7)."""

    DEFAULT_CSS = "FilterNotice { height: auto; padding: 1 1 0 1; color: $text-muted; }"

    def __init__(self) -> None:
        super().__init__(
            Text.assemble(
                "A filter is applied — ", ("f", f"bold {ACCENT}"), " shows every box again."
            )
        )


# --- dialogs ------------------------------------------------------------------


class ConfirmModal(ModalScreen[bool]):
    """A question (§7.4): Yes · No at the right edge, No focused so a stray ⏎ answers No; y, n
    and Esc answer too, and the arrows move between the buttons."""

    AUTO_FOCUS = "#no"
    BINDINGS = [
        Binding("left,up", "step(-1)", show=False),
        Binding("right,down", "step(1)", show=False),
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str, detail: str = "") -> None:
        super().__init__()
        self.question = question
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog confirm"):
            yield Static(Text(self.question, style=f"bold {HEAD}"))
            if self.detail:
                yield Static(Text(self.detail, style=DIM), classes="dialog-detail")
            with Horizontal(classes="dialog-buttons"):
                yield ActionButton("Yes", "screen.answer(True)", variant="error", id="yes")
                yield ActionButton("No", "screen.answer(False)", variant="primary", id="no")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)

    def action_step(self, delta: int) -> None:
        buttons = list(self.query(ActionButton))
        current = buttons.index(self.focused) if self.focused in buttons else 0
        buttons[(current + delta) % len(buttons)].focus()


class DetailsModal(ModalScreen[None]):
    """A failure's technical detail (§4.5): the stack trace, the API response, the kubectl output.
    Close at the right edge; Esc, q and ⏎ close it too."""

    AUTO_FOCUS = "#close"
    BINDINGS = [Binding("escape,q,enter", "close", "Close")]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text = title
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog details"):
            yield Static(Text(self.title_text, style=f"bold {ERR}"))
            with VerticalScroll(classes="details-body"):
                yield Static(Text(self.text))
            with Horizontal(classes="dialog-buttons"):
                yield ActionButton("Close", "screen.close", variant="primary", id="close")

    def action_close(self) -> None:
        self.dismiss(None)


# --- help ---------------------------------------------------------------------

HELP_ROWS = [
    ("↑ ↓", "outside a multi-line field", "select a box; the selected box shows its state"),
    ("Home  End", "outside a field", "select the first / last box"),
    ("⏎", "the list", "open the selected box: its first field or button"),
    ("⏎", "on a button", "press it"),
    ("⏎", "in a field", "the next empty field, else the Continue button"),
    ("Tab  ⇧Tab", "in a box", "move between the fields and the buttons"),
    ("← →", "on a button", "the next / previous button"),
    ("Esc", "in a box", "back to the list"),
    ("f", "outside a field", "filter to the selected box's type; again, every box"),
    ("?  F1", "", "this help; F1 also in a field"),
    ("q  ^Q", "", "quit (asks while a tool step runs); ^Q also in a field"),
]


class HelpModal(ModalScreen[None]):
    """§7.6's keys."""

    BINDINGS = [Binding("escape,question_mark,f1,q,enter", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog help"):
            yield Static(self.table())

    @staticmethod
    def table() -> Text:
        out = Text()
        out.append("Keys", style=f"bold {HEAD}")
        for key, when, does in HELP_ROWS:
            out.append("\n")
            out.append(f"{key:<15}", style=f"bold {ACCENT}")
            out.append(f"{when:<28}", style=DIM)
            out.append(does)
        out.append(
            "\n\nMouse: click a box to open it, a field to type in it, a button to press it; the "
            "wheel scrolls the list.",
            style=DIM,
        )
        return out

    def action_close(self) -> None:
        self.dismiss(None)

    def on_click(self) -> None:
        self.dismiss(None)
