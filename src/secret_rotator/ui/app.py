"""`secret-rotator ui`'s app (design §7): the status bar, a box per listed rotation with the
selected one expanded, the footer with the filter on the selected box's type (R89); a box's Start
runs its plan as the wizard of §7.4, one plan at a time (§4.3), with Retry, Abort behind its guard,
the rollback screen and Details (§4.5)."""

import datetime
import time
from collections.abc import Callable, Sequence

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Static

from secret_rotator.executor import Abandon, Outcome
from secret_rotator.listing import Rotation
from secret_rotator.model import Action, Actor, Finished, Progress, Started
from secret_rotator.openbao import OpenBaoError
from secret_rotator.opsteps import EXPIRY, ConfirmRequest, CredentialRequest, ShowRequest
from secret_rotator.session import abort_question
from secret_rotator.staging import ROLLBACK
from secret_rotator.ui.collate import screen_of
from secret_rotator.ui.item import Item, Line, LineState, Phase
from secret_rotator.ui.run import ExecutorOf, Run
from secret_rotator.ui.widgets import (
    DIM,
    ERR,
    GREYED,
    HEAD,
    OK,
    TONE,
    TONE_COLOUR,
    TONES,
    Box,
    BoxList,
    ButtonBar,
    ButtonSpec,
    ConfirmModal,
    CredentialField,
    CredentialFields,
    Description,
    DetailsModal,
    EmptyState,
    ExpiryField,
    FilterNotice,
    HelpModal,
    Instruction,
    Notice,
    Spacer,
    StateArea,
    StatusBar,
    StepLog,
    ValueBox,
    bar,
    header_colour,
    info_text,
    minutes,
    title_markup,
)

# The footer, the filter's label between its two parts.
FOOTER = (" ↑↓ select · ⏎ open · Esc back · ", " · ? help · q quit")
FILTER = "f filter · {}"
CLEAR_FILTER = "f clear filter"
# Why a box's Start is disabled (§7.5), and an external box's Done.
WAITS = "cannot start while another rotation of its leaf is in flight"
BUSY = "cannot start a rotation while a different one is in progress"
WAITS_DONE = "cannot mark it done while another rotation of its leaf is in flight"
BUSY_DONE = "cannot mark it done while a different rotation is in progress"
# What a box in flight cannot do while another runs (§7.5), by the buttons it shows.
BUSY_OF = "cannot {} while a different rotation is in progress"
NO_ROLLBACK = "no rollback: {}"  # why Abort is disabled: a finished step has no undo (§4.5)
QUIT = "A step is running. Quit?"
QUIT_RUN = "The rotation stays in flight and resumes at this step."
QUIT_ROLLBACK = "The rollback stops at this step; Retry continues it."
ROLLING_BACK = "Rolling back"  # the rollback screen's title (§7.4)
COPIED = 2.0  # seconds Copy reads `✓ Copied`
CONTINUE = "app.press_button('continue')"

VIEWS = {CredentialRequest: "credential", ShowRequest: "show", ConfirmRequest: "confirm"}
# Where a run leaves its box.
ENDED = {
    Outcome.DONE: Phase.DONE,
    Outcome.FAILED: Phase.FAILED,
    Outcome.EXITED: Phase.IN_FLIGHT,
    Outcome.CANCELLED: Phase.DUE,
    Outcome.ROLLED_BACK: Phase.ROLLED_BACK,
    Outcome.ROLLBACK_FAILED: Phase.ROLLBACK_FAILED,
}


class RotatorApp(App[None]):
    """rotations: in the order listed, which it keeps for the session (§7.5). today: what the due
    texts count from; now: the time an in-flight box's time is told in, and its zone. executor:
    a plan's executor for a renderer, under the session's lock. notify: where each failure is told
    in Telegram, None while no chat is committed. linger: the seconds a done box shows `done`, and
    a rolled-back one `rolled back`, before it leaves or is due again. A box in flight has its
    executor loaded from OpenBao when the app is made: what its Abort rolls back, or where its
    rollback stopped."""

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
        Binding("f", "filter", show=False),
        Binding("question_mark,f1", "help", show=False),
        Binding("q", "quit", show=False),
        Binding("ctrl+q", "quit", show=False, priority=True),
    ]

    def __init__(
        self,
        rotations: Sequence[Rotation],
        *,
        today: datetime.date,
        now: datetime.datetime,
        executor: ExecutorOf,
        notify: Callable[[str], None] | None = None,
        linger: float = 1.5,
    ) -> None:
        super().__init__()
        self.animation_level = "none"  # no smooth scrolling (R46)
        self.today = today
        self.now = now
        self.executor_of = executor
        self.notify = notify
        self.linger = linger
        self.items = {item.id: item for item in map(Item.of, rotations)}
        self.order = list(self.items)
        self.selected: str | None = self.order[0] if self.order else None
        self.filtered: str | None = None  # the type the list is narrowed to; only f changes it
        self.done_count = 0
        self.runs: dict[str, Run] = {}  # a box's, from its first start, or from the start
        self.active: Run | None = None  # the one plan running (§4.3)
        self.drafts: dict[str, dict[str, str]] = {}  # a credential screen's entries, by box
        self._quitting = False  # the run is being ended so the app can exit
        self._focus_into: str | None = None  # this box's first control takes focus once it can
        self._selecting = False
        for item in self.items.values():
            if item.in_flight:
                executor = self.run_of(item).executor
                executor.load()
                if item.phase is Phase.ROLLBACK_FAILED:
                    item.rollback = executor.rollback()
                    item.undone = int(executor.staging.get(ROLLBACK))

    def run_of(self, item: Item) -> Run:
        if item.id not in self.runs:
            self.runs[item.id] = Run(self, item, self.executor_of, self.notify)
        return self.runs[item.id]

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status")
        with BoxList(id="list"):
            if not self.waiting():
                yield self.empty_state()
            for rid in self.order:
                yield Box(self.items[rid])
        yield Static(self.footer(), id="footer")

    def on_mount(self) -> None:
        self.box_list.can_focus = False
        self.call_after_refresh(self._refresh_all)
        self.set_interval(0.1, self._tick)

    @property
    def box_list(self) -> BoxList:
        return self.query_one("#list", BoxList)

    def box(self, rid: str | None) -> Box | None:
        return next((box for box in self.query(Box) if box.item.id == rid), None)

    def waiting(self) -> int:
        return sum(self.items[rid].waits_on_you(self.today) for rid in self.order)

    def shown(self) -> list[str]:
        """The boxes the list shows, in its order: every one, or the filter's type's."""
        return [rid for rid in self.order if self.filtered in (None, self.items[rid].rotation.type)]

    def similar(self) -> int:
        """The boxes of the selected box's type, it included: what `f` keeps."""
        if self.selected is None:
            return 0
        wanted = self.items[self.selected].rotation.type
        return sum(self.items[rid].rotation.type == wanted for rid in self.order)

    @staticmethod
    def empty_state() -> EmptyState:
        return EmptyState(Text("Nothing waits on you.", style=f"bold {OK}"))

    def _refresh_all(self) -> None:
        for box in self.query(Box):
            self.refresh_box(box)
        self._refresh_status()

    def _refresh_status(self) -> None:
        """The status bar, and the footer, whose filter label follows the selection."""
        self.query_one("#status", StatusBar).update(
            Text.assemble(
                (" secret-rotator ui ", f"bold {HEAD}"),
                ("│ ", DIM),
                f"{self.waiting()} waiting · {self.done_count} done this session",
            )
        )
        self.query_one("#footer", Static).update(self.footer())

    def footer(self) -> Text:
        """The global keys, with the filter's label: `f clear filter` while the filter is on;
        else the count of what `f` keeps, greyed when it keeps the selected box alone (R89)."""
        if self.filtered is not None:
            label = Text(CLEAR_FILTER)
        elif (n := self.similar()) > 1:
            label = Text(FILTER.format(f"{n} similar"))
        else:
            label = Text(FILTER.format(f"{n} item{'' if n == 1 else 's'}"), style=GREYED)
        return Text.assemble(FOOTER[0], label, FOOTER[1])

    def _changed(self, item: Item) -> None:
        """Shows what changed of the item: its box, the selected box's buttons, the status bar."""
        box = self.box(item.id)
        if box is not None:
            self.refresh_box(box)
        selected = self.box(self.selected)
        if selected is not None and selected is not box:
            self.refresh_box(selected)
        self._refresh_status()

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
            self._sync_area(box, box.area)

    @staticmethod
    def view(item: Item) -> str:
        """What the state area shows: an external key's confirm; the due box's description; the
        rollback screen; the wizard's screen at its step: an operator one with its request once
        it came, before that its step's title and instruction."""
        if item.external:
            return "external"
        if item.phase is Phase.DUE:
            return "due"
        if item.rolls_back:
            return "rollback"
        if item.request is not None:
            return VIEWS[type(item.request)]
        return "operator" if item.screen.actor is Actor.OPERATOR else "tool"

    def _sync_area(self, box: Box, area: StateArea) -> None:
        item = box.item
        view = self.view(item)
        signature = (view, item.screen.index)
        actions = tuple(spec.action for spec in self.buttons(box))
        # The wizard moved on, to a new screen or to other buttons on this one: its first control
        # takes focus; back to due, the list does. A box only being selected stays as it is.
        moved = (signature, actions) != (area.signature, area.actions)
        if moved and area.signature and not self._selecting:
            self._focus_into = item.id if item.phase is not Phase.DUE else None
        area.actions = actions
        if signature != area.signature:
            area.signature = signature
            box.revealed = False
            if self.focused is not None and area in self.focused.ancestors:
                self.screen.set_focus(None)
            area.remove_children()
            area.parts = self._parts(box, view)
            area.mount_all(area.parts)
        self._update_parts(box, area)

    def _parts(self, box: Box, view: str) -> list[Widget]:
        item = box.item
        if view == "due":
            return [Description(Text(item.rotation.plan.description)), Notice(), ButtonBar()]
        if view == "tool":
            return [StepLog(), Notice(), ButtonBar()]
        if view == "rollback":
            return [Instruction(), StepLog(), Notice(), ButtonBar()]
        parts: list[Widget] = [Instruction()]
        if view == "credential":
            parts.append(CredentialFields(*self._fields(box)))
        elif view == "show":
            parts.append(ValueBox(", ".join(item.rotation.plan.target.keys)))
        return [*parts, Spacer(), StepLog(), Notice(), ButtonBar()]

    def _fields(self, box: Box) -> list[Widget]:
        """A credential field per key; for a credential that expires, its expiry, filled with
        today plus the key's interval, blank for a key without one (R91)."""
        item, request = box.item, box.item.request
        drafts = self.drafts.setdefault(item.id, {})
        fields: list[Widget] = [CredentialField(f, drafts, box.revealed) for f in request.fields]
        if request.expires:
            entries = item.rotation.plan.target.entries
            days = [d for f in request.fields if (d := entries[f.key].interval) is not None]
            prefill = (self.today + datetime.timedelta(days=min(days))).isoformat() if days else ""
            drafts.setdefault(EXPIRY, prefill)
            fields.append(ExpiryField(EXPIRY, drafts, self.today))
        return fields

    def _update_parts(self, box: Box, area: StateArea) -> None:
        item = box.item
        for instruction in area.of(Instruction):
            instruction.update(self._instruction(item))
        for log in area.of(StepLog):
            log.set_lines(item.rollback_lines() if item.rolls_back else item.visible())
            log.display = bool(log.lines)
        logged = any(log.display for log in area.of(StepLog))
        for spacer in area.of(Spacer):
            spacer.display = logged
        for notice in area.of(Notice):
            notice.update(Text("\n".join(item.said)))
            notice.display = bool(item.said)
        for value in area.of(ValueBox):
            value.show(item.request.value, box.revealed)
        for field in area.of(CredentialField):
            field.set_revealed(box.revealed)
        self.call_after_refresh(self._ensure_focus)
        for button_bar in area.of(ButtonBar):
            button_bar.set(self.buttons(box), self.progress(item), self.progress(item, True))

    def _instruction(self, item: Item) -> Text:
        """An operator screen's title and instruction, its request's once it came, else its
        step's; an external key's are its one confirm's, its notes and the runbook. The rollback
        screen's title."""
        if item.rolls_back:
            return Instruction.build(ROLLING_BACK, "", header_colour(item))
        source = item.request if item.request is not None else item.operator_step
        return Instruction.build(source.title, source.instruction, header_colour(item))

    # --- the buttons and the progress (§7.4, §7.5) --------------------------------

    def buttons(self, box: Box) -> list[ButtonSpec]:
        item = box.item
        if item.phase is Phase.DUE:
            if item.external:
                why = self.blocked(item, BUSY_DONE, WAITS_DONE)
                return [ButtonSpec(True, "Done", "done", not why, why)]
            why = self.blocked(item, BUSY, WAITS)
            return [ButtonSpec(True, "Start", "start", not why, why)]
        run = self.runs[item.id]
        busy = self.active is not None and self.active is not run
        details = ButtonSpec(False, "Details", "details")
        if item.phase is Phase.ROLLBACK_FAILED:  # the rollback is an abort already
            why = BUSY_OF.format("retry") if busy else ""
            return [ButtonSpec(True, "Retry", "retry", not why, why), details]
        if item.phase in (Phase.FAILED, Phase.IN_FLIGHT):
            verb = "retry" if item.phase is Phase.FAILED else "resume"
            blocker = run.executor.abort_blocker()
            why = BUSY_OF.format(verb if blocker else f"{verb} or abort") if busy else ""
            first = ButtonSpec(True, verb.capitalize(), verb, not why, why)
            abort = self._abort_spec(blocker, why)
            return [first, abort, details] if item.phase is Phase.FAILED else [first, abort]
        if item.external or run.aborting or item.phase not in (Phase.RUNNING, Phase.WAITING):
            return []
        abort = self._abort_spec(run.executor.abort_blocker(), "")
        waiting = item.phase is Phase.WAITING
        request = item.request
        reveal = ButtonSpec(False, "Hide" if box.revealed else "Reveal", "reveal")
        if isinstance(request, CredentialRequest):
            drafts = self.drafts.get(item.id, {})
            filled = all(drafts.get(f.key, "").strip() for f in request.fields)
            return [
                ButtonSpec(True, "Continue", "continue", waiting and filled),
                reveal,
                ButtonSpec(False, "Clear", "clear", waiting),
                abort,
            ]
        if isinstance(request, ShowRequest):
            copied = time.monotonic() < box.copied_until
            copy = ButtonSpec(False, "✓ Copied" if copied else "Copy", "copy")
            return [ButtonSpec(True, "Done", "done", waiting), reveal, copy, abort]
        if isinstance(request, ConfirmRequest):
            return [ButtonSpec(True, "Done", "done", waiting), abort]
        return [abort]

    @staticmethod
    def _abort_spec(blocker: str | None, busy: str) -> ButtonSpec:
        """Abort, disabled while a finished step has no undo, or while another plan runs."""
        why = NO_ROLLBACK.format(blocker) if blocker else busy
        return ButtonSpec(False, "Abort", "abort", not why, why)

    def blocked(self, item: Item, busy: str, waits: str) -> str:
        """Why the due box's plan cannot start: another plan runs, or its leaf has another in
        flight; '' when it can."""
        if self.active is not None:
            return busy
        return waits if self.waits(item) else ""

    def waits(self, item: Item) -> bool:
        """Another plan of its leaf is in flight (a leaf has one, staging.py): a listed one this
        session started or has not finished, or one the list does not show, which was in flight
        when the app started."""
        others = [o for o in self.items.values() if o is not item and o.leaf == item.leaf]
        if any(other.in_flight for other in others):
            return True
        flight = item.rotation.flight
        return item.rotation.waits and not any(
            (o.rotation.plan.target.kind, o.rotation.plan.target.keys) == (flight.kind, flight.keys)
            for o in others
        )

    def progress(self, item: Item, compact: bool = False) -> Text:
        """`Step n of N`, a short bar in the box's colour, `~m min left`; in a rollback `Undo n of
        N` (§4.5); compact: no bar."""
        if item.phase is Phase.DUE or item.external:
            return Text()

        def line(head: str, fraction: float, tail: Text) -> Text:
            if compact:
                return Text.assemble(head, " · ", tail)
            colour = TONE_COLOUR[TONE[item.phase]]
            return Text.assemble(head, "  ", bar(fraction, colour), "  ", tail)

        failed = item.phase in (Phase.FAILED, Phase.ROLLBACK_FAILED)
        tail = Text("failed", style=ERR) if failed else Text(f"{minutes(item.remaining)} left")
        if item.rolls_back:
            count = len(item.rollback)
            if item.phase is Phase.ROLLED_BACK:
                return line(f"Undo {count} of {count}", 1.0, Text("done"))
            n = min(item.undone + 1, count)
            return line(f"Undo {n} of {count}", item.undone / count, tail)
        count = len(item.screens)
        if item.phase is Phase.DONE:
            return line(f"Step {count} of {count}", 1.0, Text("done"))
        n = item.screen.index + 1
        return line(f"Step {n} of {count}", (n - 1) / count, tail)

    # --- focus ------------------------------------------------------------------

    def _tick(self) -> None:
        self._ensure_focus()
        run = self.active
        working = run is not None and run.item.phase in (Phase.RUNNING, Phase.ROLLING_BACK)
        if working and run.item.id == self.selected:
            box = self.box(run.item.id)
            if box is not None:
                for log in box.area.of(StepLog):
                    log.refresh()  # its spinner and elapsed times

    def _ensure_focus(self) -> None:
        """Focus is on a control of the selected box, or nowhere: the list. A box being opened
        gets its first control once it has one."""
        if not self.screen_stack or isinstance(self.screen, ModalScreen):
            return
        box = self.box(self.selected)
        # A box still composing, or being removed (the app closing), has no state area.
        areas = box.query(StateArea) if box is not None else None
        area = areas.first() if areas else None
        focused = self.focused
        if focused is not None and (
            area is None or not focused.is_attached or area not in focused.ancestors
        ):
            self.screen.set_focus(None)
            focused = None
        if focused is None and area is not None and self._focus_into == self.selected:
            control = self._first_control(box, area)
            if control is not None:
                control.focus()
                self._focus_into = None

    def _first_control(self, box: Box, area: StateArea) -> Widget | None:
        """A credential screen's first empty field, so a paste lands there, else its first
        enabled button; nothing while the screen's steps run, since its buttons change when they
        finish. A part still being removed is skipped."""
        item = box.item
        if item.phase in (Phase.RUNNING, Phase.ROLLING_BACK):
            return None
        if item.phase is Phase.WAITING:
            empty = [f for f in area.of(CredentialField) if f.is_mounted and not f.value.strip()]
            if empty:
                return empty[0].editor
        return next((b for b in area.buttons() if b.is_mounted and b.focusable), None)

    # --- selection ----------------------------------------------------------

    def select(self, rid: str) -> None:
        if rid == self.selected or rid not in self.shown():
            return
        old = self.box(self.selected)
        self.selected = rid
        self._focus_into = None
        self.screen.set_focus(None)
        if old is not None:
            self.refresh_box(old)
        new = self.box(rid)
        if new is not None:
            self._selecting = True
            try:
                self.refresh_box(new)
            finally:
                self._selecting = False
        self._refresh_status()
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
        shown = self.shown()
        index = shown.index(self.selected) + delta
        self.select(shown[max(0, min(len(shown) - 1, index))])

    def action_select_at(self, index: int) -> None:
        shown = self.shown()
        if isinstance(self.screen, ModalScreen) or not shown:
            return
        self.select(shown[index])

    def action_open(self) -> None:
        """⏎ in the list: focus moves into the selected box, to its first control. A box whose
        every button is disabled, and that has no field, is not opened."""
        if isinstance(self.screen, ModalScreen):
            return
        box = self.box(self.selected)
        if box is None:
            return
        item = box.item
        fields = item.phase is Phase.WAITING and isinstance(item.request, CredentialRequest)
        if not fields and not any(spec.enabled for spec in self.buttons(box)):
            self.bell()
            return
        self._focus_into = item.id
        self._ensure_focus()

    def action_leave(self) -> None:
        """Esc in a box: back to the list."""
        if isinstance(self.screen, ModalScreen):
            return
        self._focus_into = None
        self.screen.set_focus(None)

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_filter(self) -> None:
        """f: narrows the list to the selected box's type, or shows every box again (R89). A box
        with no others of its type has nothing to narrow to. Cleared from an emptied list, the
        first box is selected."""
        if isinstance(self.screen, ModalScreen):
            return
        if self.filtered is None and self.similar() < 2:
            self.bell()
            return
        on = self.filtered is None
        self.filtered = self.items[self.selected].rotation.type if on else None
        self._refresh_filter()
        self._refresh_status()
        if self.selected is None and self.order:
            self.select(self.order[0])
        else:
            self._scroll_to_selected()

    def _refresh_filter(self) -> None:
        """Shows the filter's boxes; a filtered list gone empty says so."""
        shown = set(self.shown())
        for box in self.query(Box):
            box.display = box.item.id in shown
        empty = self.filtered is not None and not shown
        if empty and not self.query(FilterNotice):
            self.box_list.mount(FilterNotice())
        elif not empty:
            self.query(FilterNotice).remove()

    # --- the buttons' actions -------------------------------------------------

    def action_press_button(self, action: str) -> None:
        box = self.box(self.selected)
        if box is None:
            return
        item = box.item
        if action == "start" or (action == "done" and item.phase is Phase.DUE):
            self.start(item)
        elif action in ("retry", "resume"):
            self.retry(item)
        elif action == "abort":
            self._ask_abort(item)
        elif action == "details":
            self._details(item)
        elif action == "continue":
            self._continue(box)
        elif action == "done":
            self.answer(item, {})
        elif action == "reveal":
            box.revealed = not box.revealed
            self.refresh_box(box)
        elif action == "clear":
            fields = list(box.area.of(CredentialField))
            for field in fields:
                field.set_value("")
            if fields:
                fields[0].editor.focus()
        elif action == "copy":
            self.copy_to_clipboard(item.request.value)  # OSC 52
            box.copied_until = time.monotonic() + COPIED
            self.refresh_box(box)
            self.set_timer(COPIED + 0.05, lambda: box.is_attached and self.refresh_box(box))

    def start(self, item: Item) -> None:
        """Runs the due box's plan; an external key's answers its confirm with Done, pressed."""
        if self.active is not None or self.waits(item):
            self.bell()
            return
        self._attempt(item)

    def retry(self, item: Item) -> None:
        """Retry: the failed step again, or the stopped rollback on; Resume: the plan on from
        the step it was left at."""
        if self.active is not None:
            self.bell()
            return
        self._attempt(item)

    def _attempt(self, item: Item, *, aborting: bool = False) -> None:
        run = self.run_of(item)
        run.attempt(run.executor.abort if aborting else run.executor.run, aborting=aborting)
        item.said = []
        item.phase = Phase.ROLLING_BACK if item.rolls_back else Phase.RUNNING
        self.active = run
        self._changed(item)

    def _ask_abort(self, item: Item) -> None:
        """Abort's guard (§4.5): one question, then the rollback, or a cancel while nothing has
        mutated."""
        run = self.runs[item.id]
        if self.active not in (None, run) or run.executor.abort_blocker():
            self.bell()
            return
        question = abort_question(run.executor)
        self.push_screen(ConfirmModal(question), lambda yes: self._abort(item) if yes else None)

    def _abort(self, item: Item) -> None:
        """Rolls the plan back where it now stands: an operator step abandoned, a tool step
        stopped, its undo run (Executor.stop); a plan not running, by an attempt of its own.
        Abort empties the fields."""
        run = self.runs[item.id]
        if self.active not in (None, run) or run.executor.abort_blocker():
            self.bell()
            return
        self.drafts.pop(item.id, None)
        if self.active is None:
            if item.phase in (Phase.FAILED, Phase.IN_FLIGHT):
                self._attempt(item, aborting=True)
            return
        run.aborting = True
        if item.phase is Phase.WAITING:
            self.answer(item, Abandon.ABORT)
        else:
            run.executor.stop(Abandon.ABORT)
            self._changed(item)

    def _details(self, item: Item) -> None:
        """The technical detail of the box's failure; of an earlier session's, what the run state
        recorded."""
        try:
            text = self.runs[item.id].session.details()
        except OpenBaoError as e:
            text = f"error: {e}"
        self.push_screen(DetailsModal(f"Details · {item.stopped_at()}", text))

    def answer(self, item: Item, answer: dict[str, str] | Abandon) -> None:
        """The operator's answer to the operator step the box's wizard waits on."""
        if item.phase is not Phase.WAITING:
            return
        item.phase = Phase.RUNNING
        self.drafts.pop(item.id, None)
        self.active.answer(answer)
        self._changed(item)

    def _continue(self, box: Box, confirmed: bool = False) -> None:
        """A credential screen's answer. An expiry that is not a date after today is not taken; a
        value off its shape asks first (R53)."""
        item = box.item
        if item.phase is not Phase.WAITING:
            return
        for expiry in box.area.of(ExpiryField):
            if expiry.problem():
                self.bell()
                expiry.editor.focus()
                return
        off = [(f.field.key, f.mismatch()) for f in box.area.of(CredentialField) if f.mismatch()]
        if off and not confirmed:
            names = " and ".join(key for key, _ in off)
            detail = " · ".join(f"{key}: expected {words}" for key, words in off)
            verb = "does" if len(off) == 1 else "do"
            self.push_screen(
                ConfirmModal(f"The {names} {verb} not look as expected. Continue anyway?", detail),
                lambda yes: self._continue(box, True) if yes and box.is_attached else None,
            )
            return
        request, drafts = item.request, self.drafts.get(item.id, {})
        answer = {f.key: drafts.get(f.key, "") for f in request.fields}
        if request.expires:
            answer[EXPIRY] = drafts.get(EXPIRY, "").strip()
        self.screen.set_focus(None)
        self.answer(item, answer)

    def on_credential_field_changed(self) -> None:
        box = self.box(self.selected)
        if box is not None:
            self.refresh_box(box)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """⏎ in a field: the next empty field, else focus on Continue, which a second ⏎
        presses."""
        event.stop()
        box = self.box(self.selected)
        if box is None or self._next_empty_field(box):
            return
        button = next((b for b in box.area.buttons() if b.action == CONTINUE), None)
        if button is not None and button.focusable:
            button.focus()
        else:
            self.bell()

    def _next_empty_field(self, box: Box) -> bool:
        fields = list(box.area.of(CredentialField))
        empty = [f for f in fields if not f.value.strip()]
        if not empty:
            return False
        current = next((f for f in fields if self.focused is f.editor), None)
        after = [f for f in empty if current is None or fields.index(f) > fields.index(current)]
        (after or empty)[0].editor.focus()
        return True

    # --- the run, from its thread ----------------------------------------------------

    def on_run_stepped(self, message: Run.Stepped) -> None:
        item, event = message.run.item, message.event
        key = (event.step.id, event.action)
        undo = event.action is not Action.RUN
        if undo:  # the rollback screen
            item.rollback = message.rollback
            item.phase = Phase.ROLLING_BACK
        if isinstance(event, Started):
            if undo:
                item.undone = item.rollback.index((event.step, event.action))
            else:
                if screen_of(item.screens, event.step) is not item.screen:
                    item.request = None
                item.at = item.rotation.plan.index(event.step.id)
            before = item.lines.get(key)
            attempt = before.attempt + 1 if before is not None else 1
            item.lines[key] = Line(event.step, event.action, time.monotonic(), attempt=attempt)
        elif isinstance(event, Progress):
            item.lines[key].detail = event.detail
        elif isinstance(event, Finished):
            line = item.lines[key]
            line.state = LineState.OK if event.ok else LineState.FAILED
            line.detail = event.detail if event.ok else event.detail or line.detail
            line.error = event.error
            line.elapsed = time.monotonic() - line.started
            if undo and event.ok:
                item.undone += 1
        self._changed(item)

    def on_run_asked(self, message: Run.Asked) -> None:
        run, item = message.run, message.run.item
        if self._quitting:
            run.answer(Abandon.EXIT)
        elif item.external:
            run.answer({})  # its Done, pressed to start it
        else:
            item.request = message.request
            item.phase = Phase.WAITING
            self._changed(item)

    def on_run_said(self, message: Run.Said) -> None:
        message.run.item.said.append(message.line)
        self._changed(message.run.item)

    def on_run_questioned(self, message: Run.Questioned) -> None:
        run = message.run
        if self._quitting:
            run.answer(False)
            return
        detail = run.item.said[-1] if run.item.said else ""
        self.push_screen(ConfirmModal(message.question, detail), lambda yes: run.answer(bool(yes)))

    def on_run_ended(self, message: Run.Ended) -> None:
        """outcome None: the session said why it got to none; the box stands where it did unless
        a step started, which leaves it failed, in its rollback if it was rolling back. Quitting,
        the app exits once the plan is left."""
        run, item = message.run, message.run.item
        self.active = None
        run.aborting = False
        if message.outcome is not None:
            item.phase = ENDED[message.outcome]
        elif not run.started:
            item.phase = run.before
        else:
            rolling = item.phase is Phase.ROLLING_BACK
            item.phase = Phase.ROLLBACK_FAILED if rolling else Phase.FAILED
        if self._quitting:
            self.exit()
            return
        if item.phase is Phase.DONE:
            self.set_timer(self.linger, lambda: self._remove(item))
        elif item.phase is Phase.ROLLED_BACK:
            self.set_timer(self.linger, lambda: self._due_again(item))
        elif item.phase is Phase.DUE:
            self._due_again(item)
            return
        self._changed(item)

    def _due_again(self, item: Item) -> None:
        """The box is due again, in place and still selected (design §7.3); when nothing waits, the
        green box tops the list."""
        item.due_again()
        self.drafts.pop(item.id, None)
        self._green_box()
        self._changed(item)

    def _green_box(self) -> None:
        if not self.waiting() and not self.query(EmptyState):
            self.box_list.mount(self.empty_state(), before=0)

    def _remove(self, item: Item) -> None:
        """A done box leaves the list; the box below it in the list shown is selected, the one
        above when it was the last (design §7.3). When it was the last that waits, the green box
        tops the list; when it was the filter's last, the list says a filter is applied."""
        shown = self.shown()
        self.order.remove(item.id)
        self.done_count += 1
        box = self.box(item.id)
        if box is not None:
            box.remove()
        if self.selected == item.id:
            index, rest = shown.index(item.id), self.shown()
            self.selected = rest[min(index, len(rest) - 1)] if rest else None
            self._focus_into = None
            new = self.box(self.selected)
            if new is not None:
                self.refresh_box(new)
                self._scroll_to_selected()
        self._green_box()
        self._refresh_filter()
        self._refresh_status()

    # --- quit -----------------------------------------------------------------

    def action_quit(self) -> None:
        """Quits; a plan running is left in flight where it is first, its lock released. While a
        tool step runs it asks first (§4.5); while a question is open q does not stack another."""
        if isinstance(self.screen, ConfirmModal):
            return
        if self.active is None or self.active.item.phase is Phase.WAITING:
            self._quit()
            return
        rolling = self.active.item.phase is Phase.ROLLING_BACK
        detail = QUIT_ROLLBACK if rolling else QUIT_RUN
        self.push_screen(ConfirmModal(QUIT, detail), lambda yes: self._quit() if yes else None)

    def _quit(self) -> None:
        if self.active is None:
            self.exit()
            return
        self._quitting = True
        item = self.active.item
        if item.phase is Phase.WAITING:
            self.answer(item, Abandon.EXIT)
        else:
            self.active.executor.stop(Abandon.EXIT)
