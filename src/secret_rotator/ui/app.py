"""`secret-rotator ui`'s app (design §7): the status bar, a box per listed rotation with the
selected one expanded, the footer; a box's Start runs its plan as the wizard of §7.4, one plan at a
time (§4.3)."""

import datetime
import time
from collections.abc import Sequence

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Static

from secret_rotator.executor import Abandon, Outcome
from secret_rotator.listing import Rotation
from secret_rotator.model import Action, Finished, Progress, Started
from secret_rotator.opsteps import EXPIRY, ConfirmRequest, CredentialRequest, ShowRequest
from secret_rotator.ui.collate import screen_of
from secret_rotator.ui.item import Item, Line, LineState, Phase
from secret_rotator.ui.run import ExecutorOf, Run
from secret_rotator.ui.widgets import (
    DIM,
    ERR,
    HEAD,
    OK,
    TONE,
    TONE_COLOUR,
    TONES,
    ActionButton,
    Box,
    BoxList,
    ButtonBar,
    ButtonSpec,
    ConfirmModal,
    CredentialField,
    CredentialFields,
    Description,
    EmptyState,
    ExpiryField,
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

FOOTER = " ↑↓ select · ⏎ open · Esc back · ? help · q quit"
# Why a box's Start is disabled (§7.5), and an external box's Done.
WAITS = "cannot start while another rotation of its leaf is in flight"
BUSY = "cannot start a rotation while a different one is in progress"
WAITS_DONE = "cannot mark it done while another rotation of its leaf is in flight"
BUSY_DONE = "cannot mark it done while a different rotation is in progress"
COPIED = 2.0  # seconds Copy reads `✓ Copied`
CONTINUE = "app.press_button('continue')"

VIEWS = {CredentialRequest: "credential", ShowRequest: "show", ConfirmRequest: "confirm"}
# Where a run leaves its box.
ENDED = {Outcome.DONE: Phase.DONE, Outcome.FAILED: Phase.FAILED, Outcome.EXITED: Phase.IN_FLIGHT}


class RotatorApp(App[None]):
    """rotations: in the order listed, which it keeps for the session (§7.5). today: what the due
    texts count from; now: the time an in-flight box's time is told in, and its zone. executor:
    a plan's executor for a renderer, under the session's lock. linger: the seconds a done box
    shows `done` before it leaves."""

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
        self,
        rotations: Sequence[Rotation],
        *,
        today: datetime.date,
        now: datetime.datetime,
        executor: ExecutorOf,
        linger: float = 1.5,
    ) -> None:
        super().__init__()
        self.animation_level = "none"  # no smooth scrolling (R46)
        self.today = today
        self.now = now
        self.executor_of = executor
        self.linger = linger
        self.items = {item.id: item for item in map(Item.of, rotations)}
        self.order = list(self.items)
        self.selected: str | None = self.order[0] if self.order else None
        self.done_count = 0
        self.active: Run | None = None  # the one plan running (§4.3)
        self.drafts: dict[str, dict[str, str]] = {}  # a credential screen's entries, by box
        self._quitting = False  # the run is being ended so the app can exit
        self._focus_into: str | None = None  # this box's first control takes focus once it can
        self._selecting = False

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status")
        with BoxList(id="list"):
            if not self.waiting():
                yield self.empty_state()
            for rid in self.order:
                yield Box(self.items[rid])
        yield Static(Text(FOOTER), id="footer")

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

    @staticmethod
    def empty_state() -> EmptyState:
        return EmptyState(Text("Nothing waits on you.", style=f"bold {OK}"))

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
        wizard's screen, an operator one once its request came; nothing for a box in flight or
        whose rollback failed (P9)."""
        if item.external:
            return "external"
        if item.phase is Phase.DUE:
            return "due"
        if item.phase in (Phase.IN_FLIGHT, Phase.ROLLBACK_FAILED):
            return ""
        return VIEWS.get(type(item.request), "tool")

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
        if view == "":
            return [Notice()]
        if view == "tool":
            return [StepLog(), Notice(), ButtonBar()]
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
            log.set_lines(item.visible())
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
        """An operator screen's title and instruction; an external key's are its one confirm's,
        its notes and the runbook."""
        source = item.rotation.plan.steps[0] if item.external else item.request
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
        if item.external or item.phase not in (Phase.RUNNING, Phase.WAITING):
            return []
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
            ]
        if isinstance(request, ShowRequest):
            copied = time.monotonic() < box.copied_until
            copy = ButtonSpec(False, "✓ Copied" if copied else "Copy", "copy")
            return [ButtonSpec(True, "Done", "done", waiting), reveal, copy]
        if isinstance(request, ConfirmRequest):
            return [ButtonSpec(True, "Done", "done", waiting)]
        return []

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
        """`Step n of N`, a short bar in the box's colour, `~m min left`; compact: no bar."""
        if item.phase is Phase.DUE or item.external:
            return Text()

        def line(head: str, fraction: float, tail: Text) -> Text:
            if compact:
                return Text.assemble(head, " · ", tail)
            colour = TONE_COLOUR[TONE[item.phase]]
            return Text.assemble(head, "  ", bar(fraction, colour), "  ", tail)

        count = len(item.screens)
        if item.phase is Phase.DONE:
            return line(f"Step {count} of {count}", 1.0, Text("done"))
        n = item.screen.index + 1
        failed = item.phase is Phase.FAILED
        tail = Text("failed", style=ERR) if failed else Text(f"{minutes(item.remaining)} left")
        return line(f"Step {n} of {count}", (n - 1) / count, tail)

    # --- focus ------------------------------------------------------------------

    def _tick(self) -> None:
        self._ensure_focus()
        run = self.active
        if run is not None and run.item.phase is Phase.RUNNING and run.item.id == self.selected:
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
        if item.phase is Phase.RUNNING:
            return None
        if item.phase is Phase.WAITING:
            empty = [f for f in area.of(CredentialField) if f.is_mounted and not f.value.strip()]
            if empty:
                return empty[0].editor
        return next(
            (b for b in area.of(ActionButton) if b.is_mounted and b.focusable),
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
            self._selecting = True
            try:
                self.refresh_box(new)
            finally:
                self._selecting = False
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

    # --- the buttons' actions -------------------------------------------------

    def action_press_button(self, action: str) -> None:
        box = self.box(self.selected)
        if box is None:
            return
        item = box.item
        if action == "start" or (action == "done" and item.phase is Phase.DUE):
            self.start(item)
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
        item.said = []
        item.phase = Phase.RUNNING
        self.active = Run(self, item, self.executor_of)
        self.active.start()
        self._changed(item)

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
        button = next((b for b in box.area.of(ActionButton) if b.action == CONTINUE), None)
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
        if isinstance(event, Started):
            if event.action is Action.RUN:
                if screen_of(item.screens, event.step) is not item.screen:
                    item.request = None
                item.at = item.rotation.plan.index(event.step.id)
            item.lines[key] = Line(event.step, event.action, time.monotonic())
        elif isinstance(event, Progress):
            item.lines[key].detail = event.detail
        elif isinstance(event, Finished):
            line = item.lines[key]
            line.state = LineState.OK if event.ok else LineState.FAILED
            line.detail = event.detail if event.ok else event.detail or line.detail
            line.error = event.error
            line.elapsed = time.monotonic() - line.started
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
        """outcome None: the session said why it got to none, a failure once a step started.
        Quitting, the app exits once the plan is left."""
        run, item = message.run, message.run.item
        self.active = None
        if message.outcome is None:
            item.phase = Phase.FAILED if run.started else Phase.DUE
        else:
            item.phase = ENDED[message.outcome]
        if self._quitting:
            self.exit()
            return
        if item.phase is Phase.DONE:
            self.set_timer(self.linger, lambda: self._remove(item))
        self._changed(item)

    def _remove(self, item: Item) -> None:
        """A done box leaves the list; the box below it is selected, the one above when it was
        the last (D20). When it was the last that waits, the green box tops the list."""
        index = self.order.index(item.id)
        self.order.remove(item.id)
        self.done_count += 1
        box = self.box(item.id)
        if box is not None:
            box.remove()
        if self.selected == item.id:
            self.selected = self.order[min(index, len(self.order) - 1)] if self.order else None
            self._focus_into = None
            new = self.box(self.selected)
            if new is not None:
                self.refresh_box(new)
                self._scroll_to_selected()
        if not self.waiting() and not self.query(EmptyState):
            self.box_list.mount(self.empty_state(), before=0)
        self._refresh_status()

    # --- quit -----------------------------------------------------------------

    def action_quit(self) -> None:
        """Quits; a plan running is left in flight where it is first, its lock released. While a
        question is open q does not stack another."""
        if isinstance(self.screen, ConfirmModal):
            return
        if self.active is None:
            self.exit()
            return
        self._quitting = True
        item = self.active.item
        if item.phase is Phase.WAITING:
            self.answer(item, Abandon.EXIT)
        else:
            self.active.executor.stop(Abandon.EXIT)
