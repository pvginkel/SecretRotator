"""The UI's widgets (design §7.2): the status bar, the boxes, their state area's parts, help."""

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from secret_rotator.nightly import HORIZON
from secret_rotator.ui.collate import screen_of
from secret_rotator.ui.item import Item, Phase

if TYPE_CHECKING:
    from secret_rotator.ui.app import RotatorApp

# textual-dark's palette, so Rich text matches the stylesheet's $accent, $text-accent, …
ACCENT = "#FEA62B"  # $accent
HEAD = "#FFC473"  # $text-accent: the app name, a due date within the first Telegram warning
OK = "#8AD4A1"  # $text-success
ERR = "#D17E92"  # $text-error
DIM = "#8a8f98"

GLYPH = {Phase.DUE: "○", Phase.IN_FLIGHT: "◐", Phase.FAILED: "✗", Phase.ROLLBACK_FAILED: "✗"}

# A box's border and title colour, selected or not (§7.2): untouched light grey, left in flight
# orange, failed red. Each is a class of the stylesheet's.
TONE = {
    Phase.DUE: "due",
    Phase.IN_FLIGHT: "accent",
    Phase.FAILED: "error",
    Phase.ROLLBACK_FAILED: "error",
}
TONES = tuple(dict.fromkeys(TONE.values()))


# --- formatting ---------------------------------------------------------------


def minutes(seconds: int) -> str:
    """An estimate rounded to the minute, half up; `<1 min` under 30 s (§7.4)."""
    return "<1 min" if seconds < 30 else f"~{int(seconds / 60 + 0.5)} min"


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
    """The information line (§7.2, §7.3)."""
    r = item.rotation
    if item.phase is Phase.IN_FLIGHT:
        return Text(f"{since_text(r.flight.since, now)} · {r.plan.ask} · {minutes(r.estimate)}")
    due = due_part(r.due_at, today)
    if item.phase is Phase.FAILED:
        step = r.plan.steps[r.at]
        n = screen_of(item.screens, step).index + 1
        failed = f"failed at step {n} of {len(item.screens)} · {step.title}"
        return Text.assemble(due, " · ", (failed, ERR))
    if item.phase is Phase.ROLLBACK_FAILED:
        return Text.assemble(due, " · ", ("rollback failed", ERR))
    return Text.assemble(due, f" · {r.plan.ask} · {minutes(r.estimate)}")


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

    def action_step(self, delta: int) -> None:
        """← → move between the buttons."""
        buttons = [b for b in self.query(ActionButton) if b.focusable]
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
        buttons = list(self.query(ActionButton))
        if [b.action for b in buttons] == [f"app.press_button('{s.action}')" for s in ordered]:
            for button, spec in zip(buttons, ordered, strict=True):  # a focused one keeps focus
                button.update_from(spec)
        else:
            with self.app.batch_update():
                self.query(ActionButton).remove()
                self.mount_all([ActionButton.of(s) for s in ordered], before=reason)
        reason.update(Text(self.reason()))
        # Short of room it drops the progress bar; the reason then shortens with an ellipsis.
        used = sum(ActionButton.width_of(s) for s in self.specs) + len(self.reason()) + 2
        fits = not self.size.width or used + self.progress.cell_len <= self.size.width
        progress.update(self.progress if fits else self.compact)


# --- the list -----------------------------------------------------------------


class Description(Static):
    DEFAULT_CSS = "Description { height: auto; }"


class StateArea(Vertical):
    """What a selected box shows under its information line (§7.2)."""

    DEFAULT_CSS = "StateArea { height: auto; padding-top: 1; }"

    def __init__(self) -> None:
        super().__init__()
        self.signature: tuple = ()


class Box(Vertical):
    """One rotation: a full box whatever its state, its state area shown iff it is selected
    (R26)."""

    def __init__(self, item: Item) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        yield Static(classes="info")
        yield StateArea()

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
