"""`secret-rotator ui`'s app (design §7): the status bar, a box per listed rotation with the
selected one expanded, the footer."""

import datetime
from collections.abc import Sequence

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

from secret_rotator.listing import Rotation
from secret_rotator.ui.item import Item, Phase
from secret_rotator.ui.widgets import (
    DIM,
    HEAD,
    OK,
    TONE,
    TONES,
    ActionButton,
    Box,
    BoxList,
    ButtonBar,
    ButtonSpec,
    Description,
    EmptyState,
    HelpModal,
    StateArea,
    StatusBar,
    info_text,
    title_markup,
)

FOOTER = " ↑↓ select · ⏎ open · Esc back · ? help · q quit"
WAITS = "cannot start while another rotation of its leaf is in flight"


class RotatorApp(App[None]):
    """rotations: in the order listed, which it keeps for the session (§7.5). today: what the due
    texts count from; now: the time an in-flight box's time is told in, and its zone."""

    CSS_PATH = "app.tcss"
    TITLE = "secret-rotator ui"
    ENABLE_COMMAND_PALETTE = False

    # §7.6: the list has the keys until a box is opened; a focused button or field takes ⏎ and
    # what it types itself. Letters type in a field, so ? and q have F1 and ^Q twins.
    BINDINGS = [
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("home", "select_at(0)", show=False),
        Binding("end", "select_at(-1)", show=False),
        Binding("enter", "open", show=False),
        Binding("escape", "leave", show=False),
        Binding("question_mark,f1", "help", show=False),
        Binding("q", "quit", show=False),
        Binding("ctrl+q", "quit", show=False, priority=True),
    ]

    def __init__(
        self, rotations: Sequence[Rotation], *, today: datetime.date, now: datetime.datetime
    ) -> None:
        super().__init__()
        self.animation_level = "none"  # no smooth scrolling (R46)
        self.today = today
        self.now = now
        self.items = {item.id: item for item in map(Item.of, rotations)}
        self.order = list(self.items)
        self.selected: str | None = self.order[0] if self.order else None
        self.done_count = 0
        self._focus_into: str | None = None  # this box's first control takes focus once it can

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status")
        with BoxList(id="list"):
            if not self.waiting():
                yield EmptyState(Text("Nothing waits on you.", style=f"bold {OK}"))
            for rid in self.order:
                yield Box(self.items[rid])
        yield Static(Text(FOOTER), id="footer")

    def on_mount(self) -> None:
        self.box_list.can_focus = False
        self.call_after_refresh(self._refresh_all)
        self.set_interval(0.1, self._ensure_focus)

    @property
    def box_list(self) -> BoxList:
        return self.query_one("#list", BoxList)

    def box(self, rid: str | None) -> Box | None:
        return next((box for box in self.query(Box) if box.item.id == rid), None)

    def waiting(self) -> int:
        return sum(self.items[rid].waits_on_you(self.today) for rid in self.order)

    def _refresh_all(self) -> None:
        for box in self.query(Box):
            self.refresh_box(box)
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.query_one("#status", StatusBar).update(
            Text.assemble(
                (" secret-rotator ui ", f"bold {HEAD}"),
                ("│ ", DIM),
                f"{self.waiting()} waiting · {self.done_count} done this session",
            )
        )

    # --- one box ------------------------------------------------------------

    def refresh_box(self, box: Box) -> None:
        item = box.item
        selected = item.id == self.selected
        box.set_class(selected, "-selected")
        for tone in TONES:
            box.set_class(TONE[item.phase] == tone, f"-{tone}")
        box.border_title = title_markup(item, selected)
        box.query_one(".info", Static).update(info_text(item, self.today, self.now))
        if selected:
            self._sync_area(box, box.query_one(StateArea))

    def _sync_area(self, box: Box, area: StateArea) -> None:
        signature = (box.item.phase,)
        if signature != area.signature:
            area.signature = signature
            if self.focused is not None and area in self.focused.ancestors:
                self.screen.set_focus(None)
            area.remove_children()
            area.mount_all(self._parts(box.item))
        for button_bar in area.query(ButtonBar):
            button_bar.set(self.buttons(box.item), Text())

    def _parts(self, item: Item) -> list[Widget]:
        if item.phase is Phase.DUE:
            return [Description(Text(item.rotation.plan.description)), ButtonBar()]
        return []

    def buttons(self, item: Item) -> list[ButtonSpec]:
        if item.phase is Phase.DUE:
            waits = item.rotation.waits
            return [ButtonSpec(True, "Start", "start", not waits, WAITS if waits else "")]
        return []

    def _ensure_focus(self) -> None:
        """Focus is on a control of the selected box, or nowhere: the list. A box being opened
        gets its first control once it has one."""
        if not self.screen_stack or isinstance(self.screen, ModalScreen):
            return
        box = self.box(self.selected)
        area = box.query(StateArea).first() if box is not None else None
        focused = self.focused
        if focused is not None and (
            area is None or not focused.is_attached or area not in focused.ancestors
        ):
            self.screen.set_focus(None)
            focused = None
        if focused is None and area is not None and self._focus_into == self.selected:
            control = self._first_control(area)
            if control is not None:
                control.focus()
                self._focus_into = None

    def _first_control(self, area: StateArea) -> Widget | None:
        """Its first enabled button; a part still being removed is skipped."""
        return next(
            (b for b in area.query(ActionButton) if b.display and b.is_mounted and b.focusable),
            None,
        )

    # --- selection ----------------------------------------------------------

    def select(self, rid: str) -> None:
        if rid == self.selected or rid not in self.order:
            return
        old = self.box(self.selected)
        self.selected = rid
        self._focus_into = None
        self.screen.set_focus(None)
        if old is not None:
            self.refresh_box(old)
        new = self.box(rid)
        if new is not None:
            self.refresh_box(new)
        self._scroll_to_selected()

    def _scroll_to_selected(self) -> None:
        """Lays out and scrolls, painting nothing in between: else the list is drawn at the old
        position with the new box expanded, then jumps (R63)."""
        batch = self.batch_update()
        batch.__enter__()

        def scroll() -> None:
            try:
                box = self.box(self.selected)
                if box is not None:
                    # Textual lays nothing out while a batch is open (Screen._on_timer_update),
                    # and paints the layout last made when it closes.
                    self.screen._refresh_layout()
                    self.box_list.scroll_to_widget(box, animate=False, immediate=True)
                    self.screen._refresh_layout(scroll=True)
            finally:
                batch.__exit__(None, None, None)

        self.call_after_refresh(scroll)

    def action_move(self, delta: int) -> None:
        if isinstance(self.screen, ModalScreen) or self.selected is None:
            return
        index = self.order.index(self.selected) + delta
        self.select(self.order[max(0, min(len(self.order) - 1, index))])

    def action_select_at(self, index: int) -> None:
        if isinstance(self.screen, ModalScreen) or not self.order:
            return
        self.select(self.order[index])

    def action_open(self) -> None:
        """⏎ in the list: focus moves into the selected box, to its first control. A box whose
        every button is disabled is not opened."""
        if isinstance(self.screen, ModalScreen):
            return
        box = self.box(self.selected)
        if box is None:
            return
        if not any(spec.enabled for spec in self.buttons(box.item)):
            self.bell()
            return
        self._focus_into = box.item.id
        self._ensure_focus()

    def action_leave(self) -> None:
        """Esc in a box: back to the list."""
        if isinstance(self.screen, ModalScreen):
            return
        self._focus_into = None
        self.screen.set_focus(None)

    def action_help(self) -> None:
        self.push_screen(HelpModal())
