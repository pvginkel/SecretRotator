"""`secret-rotator ui`'s list (design §7.2, §7.3, §7.5-§7.7), the app run headless over the listing
of the fake OpenBao: every box a full box, expanded iff selected, its title, its information line
in each state the list starts in, its colour; the status bar, the due box's state area, the keys,
the mouse, the help and the empty state; and the colour over the VS Code task's chain."""

import asyncio
import contextlib
import datetime
import os
import time

from fake_openbao import WRITTEN_AT
from plans import client, fake_of, lock, put_flight, put_state, run_state
from rich.text import Text
from test_listing import (
    BOT,
    HOOK,
    IAC_AGENT,
    MIXED,
    OPENBAO_ADMIN,
    PAT,
    SEAL,
    SHOP,
    WIFI,
    listing,
    store,
)
from textual.screen import Screen
from textual.widgets import Static

from secret_rotator.executor import Executor
from secret_rotator.model import Actor
from secret_rotator.staging import ROLLBACK
from secret_rotator.ui import full_colour
from secret_rotator.ui.app import WAITS, RotatorApp
from secret_rotator.ui.collate import collate
from secret_rotator.ui.widgets import (
    ERR,
    HEAD,
    HELP_ROWS,
    ActionButton,
    Box,
    Description,
    EmptyState,
    HelpModal,
    StateArea,
    StatusBar,
    due_part,
    minutes,
    since_text,
)

TODAY = datetime.date(2026, 10, 8)
NOW = datetime.datetime(2026, 10, 8, 9, 0, tzinfo=datetime.UTC)
SIZE = (110, 60)

FAILED = f"{MIXED}#password"  # failed at its write
WAITING = f"{MIXED}#pin"  # its leaf's other plan is in flight
IN_FLIGHT = f"{OPENBAO_ADMIN}#secret_id"  # left at its show
ORDER = [
    FAILED,  # due now
    IN_FLIGHT,  # due 2026-11-30
    f"{BOT}#telegram-bot-token",  # due now: never stamped
    WAITING,  # due now
    f"{PAT}#token",  # overdue 37d
    f"{HOOK}#secret",  # due today
    f"{IAC_AGENT}#secret_id",  # due in 10 days
    f"{SHOP}#api-key",  # due in 137 days: its expiry less 7d
    f"{SEAL}#seal-key",  # due in 205 days
    f"{WIFI}#password",  # no due date
]


def world():
    """The listing's store with a box in each state the list starts in: manual keys typed and not,
    approle's manual deliveries, external, random with a confirm, a key without a due date."""
    bao = fake_of(store())
    put_state(bao, PAT, stamps={"token": "2025-09-01"})
    put_state(bao, HOOK, stamps={"secret": "2026-09-08"})
    put_state(bao, IAC_AGENT, stamps={"secret_id": "2026-07-20"})
    put_state(bao, OPENBAO_ADMIN, stamps={"secret_id": "2026-09-01"})
    put_state(bao, SEAL, stamps={"seal-key": "2026-05-01"})
    put_flight(bao, "manual", MIXED, ["password"], "kv.write")
    put_state(bao, MIXED, status="failed", last_error="kv.write: HTTP 403")
    put_flight(bao, "approle", OPENBAO_ADMIN, ["secret_id"], "operator.show:approle:secret-id")
    return bao


def executor_of(bao):
    """The executor of a plan on the fake, under the UI's lock, its clock at NOW."""

    def executor(plan, renderer):
        return Executor(
            client(bao),
            plan,
            renderer,
            lock(bao, "secret-rotator ui"),
            state=run_state(bao),
            dry_run=False,
            clock=lambda: NOW,
        )

    return executor


def app_of(bao, today=TODAY, now=NOW, rotations=None, notify=None):
    rotations = listing(bao) if rotations is None else rotations
    executor = executor_of(bao)
    return RotatorApp(
        rotations, today=today, now=now, executor=executor, notify=notify, linger=0.05
    )


async def until(pilot, condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out; focused={pilot.app.focused!r}"
        await pilot.pause(0.02)


async def settled(pilot):
    await pilot.pause(0.2)


@contextlib.contextmanager
def slow_loop(lag=0.1):
    """Each turn of the running event loop blocks for `lag` seconds, the app's focus timer's
    interval: the timer, which runs in a task of its own, is then due between any two turns, also
    while the app waits on a widget's removal."""
    loop = asyncio.get_running_loop()
    handle = None

    def turn():
        nonlocal handle
        time.sleep(lag)
        handle = loop.call_soon(turn)

    handle = loop.call_soon(turn)
    try:
        yield
    finally:
        handle.cancel()


async def select(pilot, app, rid):
    app.select(rid)
    await pilot.pause(0.05)


def info(app, rid) -> Text:
    return app.box(rid).query_one(".info", Static).content


def in_box(app):
    box = app.box(app.selected)
    return box is not None and app.focused is not None and box in app.focused.ancestors


def tones(box):
    return {t for t in ("due", "accent", "error") if box.has_class(f"-{t}")}


def styles(text: Text) -> dict[str, str]:
    return {text.plain[s.start : s.end]: str(s.style) for s in text.spans}


# --- the formatting -------------------------------------------------------------


def test_the_due_text_says_how_near_the_due_date_is_in_its_colour():
    def due(at):
        text = due_part(at, TODAY)
        return text.plain, str(text.style)

    assert due(datetime.date(2026, 9, 1)) == ("overdue 37d", ERR)
    assert due(TODAY) == ("due today", ERR)
    assert due(datetime.date.min) == ("due now", ERR)  # never stamped
    assert due(None) == ("no due date configured", ERR)
    assert due(datetime.date(2026, 10, 9)) == ("due in 1 day", HEAD)
    assert due(datetime.date(2026, 11, 5)) == ("due in 28 days", HEAD)  # the first warning
    assert due(datetime.date(2026, 11, 6)) == ("due in 29 days", "")


def test_in_flight_since_is_told_in_the_zone_of_now():
    assert since_text(WRITTEN_AT, NOW) == "in flight since 4 Oct 21:14"
    assert since_text(WRITTEN_AT, WRITTEN_AT + datetime.timedelta(hours=3)) == (
        "in flight since yesterday 21:14"  # 00:14 the next day
    )
    assert since_text(WRITTEN_AT, WRITTEN_AT + datetime.timedelta(minutes=1)) == (
        "in flight since 21:14"
    )
    amsterdam = datetime.timezone(datetime.timedelta(hours=2))
    assert since_text(WRITTEN_AT, NOW.astimezone(amsterdam)) == "in flight since 4 Oct 23:14"


def test_an_estimate_reads_to_the_minute_half_up():
    assert [minutes(s) for s in (0, 29, 30, 89, 90, 150)] == [
        "<1 min",
        "<1 min",
        "~1 min",
        "~1 min",
        "~2 min",
        "~3 min",
    ]


def test_the_screens_collate_the_plans_steps():
    """Every operator step a screen; consecutive non-silent tool steps one; a silent step rides on
    the screen before it, or after it when it leads the plan (§7.4)."""
    plans = {r.plan.target.leaf: r.plan for r in listing(fake_of(store()))}

    def shape(leaf):
        return [(s.actor, [step.type for step in s.steps]) for s in collate(plans[leaf].steps)]

    assert shape(PAT) == [
        (Actor.OPERATOR, ["operator.credential"]),
        (Actor.TOOL, ["kv.write", "kv.stamp"]),
    ]
    assert shape(IAC_AGENT) == [
        (Actor.TOOL, ["approle.marker", "approle.mint", "approle.login"]),
        (Actor.OPERATOR, ["operator.show"]),
        (Actor.TOOL, ["kv.write", "approle.destroy_old_accessor", "kv.stamp"]),
    ]
    assert shape(HOOK) == [
        (Actor.TOOL, ["random.generate", "kv.write"]),
        (Actor.OPERATOR, ["operator.confirm", "kv.stamp"]),  # the stamp rides on the last screen
    ]
    assert shape(SEAL) == [(Actor.OPERATOR, ["operator.confirm", "kv.stamp"])]
    assert [s.index for s in collate(plans[IAC_AGENT].steps)] == [0, 1, 2]


# --- colour -----------------------------------------------------------------------


def test_full_colour_unless_the_environment_names_a_colour_system():
    env = {"TERM": "xterm"}
    full_colour(env)
    assert env == {"TERM": "xterm", "COLORTERM": "truecolor"}
    for named in ({"COLORTERM": "8bit"}, {"TEXTUAL_COLOR_SYSTEM": "standard"}):
        env = dict(named)
        full_colour(env)
        assert env == named


def test_the_app_draws_in_full_colour_over_the_vs_code_task_s_chain(monkeypatch):
    """The container the task reaches sees TERM=xterm and no COLORTERM (R95)."""
    monkeypatch.setenv("TERM", "xterm")
    for name in ("COLORTERM", "TEXTUAL_COLOR_SYSTEM"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    assert app_of(world()).console.color_system == "standard"  # what Textual would draw
    full_colour(os.environ)
    assert app_of(world()).console.color_system == "truecolor"


# --- the list ----------------------------------------------------------------------


async def test_every_box_is_a_full_box_expanded_iff_selected():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        boxes = list(app.query(Box))
        # in the order listed; nothing without an operator step, nothing that cannot be built
        assert [box.item.id for box in boxes] == app.order == ORDER
        for box in boxes:
            assert box.item.id in str(box.border_title)
            assert box.query_one(StateArea).display == (box.item.id == app.selected)
        assert app.selected == FAILED  # in flight and failed first
        await pilot.press("down")
        assert app.selected == IN_FLIGHT
        assert app.box(IN_FLIGHT).query_one(StateArea).display
        assert not app.box(FAILED).query_one(StateArea).display


async def test_the_title_is_the_glyph_the_key_and_its_credential_in_words():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        titles = {box.item.id: str(box.border_title) for box in app.query(Box)}
        assert titles[f"{PAT}#token"] == f"○ {PAT}#token · GitHub personal access token"
        assert titles[f"{IAC_AGENT}#secret_id"] == f"○ {IAC_AGENT}#secret_id · AppRole secret_id"
        assert titles[f"{SEAL}#seal-key"] == f"○ {SEAL}#seal-key · external"
        assert titles[f"{BOT}#telegram-bot-token"] == f"○ {BOT}#telegram-bot-token · manual"
        assert titles[IN_FLIGHT] == f"◐ {IN_FLIGHT} · AppRole secret_id"
        assert titles[FAILED] == f"✗ [b]{FAILED}[/b] · manual"  # selected: its path in bold


async def test_the_information_line_of_each_state_with_the_due_text_in_its_colour():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        lines = {rid: info(app, rid).plain for rid in app.order}
        assert lines == {
            FAILED: "due now · failed at step 2 of 2 · write eso/prd/mixed/prd/creds",
            IN_FLIGHT: "in flight since 4 Oct 21:14 · put the new openbao-admin secret_id in "
            "place · <1 min",
            f"{BOT}#telegram-bot-token": "due now · paste a new telegram-bot-token · <1 min",
            WAITING: "due now · paste a new pin · <1 min",
            f"{PAT}#token": "overdue 37d · paste a new GitHub personal access token · <1 min",
            f"{HOOK}#secret": "due today · tell Bob · <1 min",
            f"{IAC_AGENT}#secret_id": "due in 10 days · put the new iac-agent secret_id in place "
            "· <1 min",
            f"{SHOP}#api-key": "due in 137 days · paste a new api-key · <1 min",
            f"{SEAL}#seal-key": "due in 205 days · rotate it outside the tool · <1 min",
            f"{WIFI}#password": "no due date configured · paste a new password · <1 min",
        }
        assert styles(info(app, f"{PAT}#token"))["overdue 37d"] == ERR
        assert styles(info(app, f"{IAC_AGENT}#secret_id"))["due in 10 days"] == HEAD
        assert "due in 137 days" not in styles(info(app, f"{SHOP}#api-key"))  # plain
        assert styles(info(app, f"{WIFI}#password"))["no due date configured"] == ERR
        failed = styles(info(app, FAILED))
        assert failed["failed at step 2 of 2 · write eso/prd/mixed/prd/creds"] == ERR


async def test_a_rollback_stopped_part_way_shows_failed_at_its_undo():
    bao = world()
    put_flight(bao, "manual", PAT, ["token"], "kv.write", **{ROLLBACK: "0"})
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        box = app.box(f"{PAT}#token")
        assert str(box.border_title).startswith("✗ ") and tones(box) == {"error"}
        line = "overdue 37d · rollback failed at undo: write eso/prd/gh/prd/pat"
        assert info(app, f"{PAT}#token").plain == line


async def test_every_box_takes_its_state_s_colour_selected_or_not():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        assert app.animation_level == "none"  # no smooth scrolling
        assert tones(app.box(FAILED)) == {"error"}
        assert tones(app.box(IN_FLIGHT)) == {"accent"}
        for rid in ORDER[2:]:  # due, not yet due or without a due date: one colour
            assert tones(app.box(rid)) == {"due"}, rid
        await select(pilot, app, IN_FLIGHT)
        assert tones(app.box(IN_FLIGHT)) == {"accent"} and tones(app.box(FAILED)) == {"error"}
        await select(pilot, app, f"{PAT}#token")
        assert tones(app.box(f"{PAT}#token")) == {"due"}


async def test_the_status_bar_counts_what_is_due_in_flight_or_failed():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        status = app.query_one("#status", StatusBar).content.plain
        assert status == " secret-rotator ui │ 6 waiting · 0 done this session"
        assert not app.query(EmptyState)


async def test_a_due_box_shows_its_plan_s_description_and_start():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, f"{PAT}#token")
        box = app.box(f"{PAT}#token")
        await until(pilot, lambda: bool(box.query(ActionButton)))
        description = box.query_one(Description).content.plain
        assert description == box.item.rotation.plan.description
        assert description.startswith("You mint a new GitHub personal access token")
        (start,) = box.query(ActionButton)
        assert start.label.plain == "Start" and not start.disabled


async def test_a_box_whose_leaf_has_another_plan_in_flight_says_it_cannot_start():
    app = app_of(world())
    bells = []
    app.bell = lambda: bells.append(1)
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, WAITING)
        box = app.box(WAITING)
        await until(pilot, lambda: bool(box.query(ActionButton)))
        (start,) = box.query(ActionButton)
        assert start.disabled
        assert box.query_one(".reason", Static).content.plain == WAITS
        await pilot.press("enter")  # its only button is disabled: it is not opened
        await settled(pilot)
        assert bells == [1] and app.focused is None


# --- keys and the mouse ---------------------------------------------------------------


async def test_enter_opens_the_box_on_its_first_button_and_esc_goes_back_to_the_list():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, f"{PAT}#token")
        await settled(pilot)
        assert app.focused is None  # selecting does not open: the list has the keys
        await pilot.press("enter")
        await until(pilot, lambda: in_box(app))
        assert app.focused.label.plain == "Start"
        await pilot.press("escape")
        assert app.focused is None
        await pilot.pause(0.3)
        assert app.focused is None  # and nothing pulls focus back in
        await pilot.press("down")
        assert app.selected == f"{HOOK}#secret"


async def test_focus_leaves_a_selected_box_that_has_no_state_area():
    """A box still composing, or being removed as the app closes, has no state area; the focus
    timer ticks through it."""
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, f"{PAT}#token")
        await pilot.press("enter")
        await until(pilot, lambda: in_box(app))
        await app.box(app.selected).query_one(StateArea).remove()
        app._ensure_focus()
        assert app.focused is None


async def test_up_down_home_and_end_select_even_with_a_button_focused():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, f"{PAT}#token")
        await pilot.press("enter")
        await until(pilot, lambda: in_box(app))
        await pilot.press("end")
        assert app.selected == ORDER[-1] and app.focused is None
        await pilot.press("down")
        assert app.selected == ORDER[-1]  # the last stays selected
        await pilot.press("home")
        assert app.selected == ORDER[0]
        await pilot.press("up")
        assert app.selected == ORDER[0]
        await pilot.press("down", "down")
        assert app.selected == ORDER[2]


async def test_every_paint_after_a_selection_shows_the_box_whole_on_a_list_taller_than_the_terminal(
    monkeypatch,
):
    app = app_of(world())
    painted = []
    paint = Screen._compositor_refresh

    def spy(screen):
        if screen is app.screen and not app._batch_count:
            painted.append(app.box(app.selected).region)
        paint(screen)

    async with app.run_test(size=(110, 16)) as pilot:
        await settled(pilot)
        monkeypatch.setattr(Screen, "_compositor_refresh", spy)
        view = app.box_list.region
        presses = ["down"] * (len(ORDER) - 1) + ["home", "end"] + ["up"] * (len(ORDER) - 1)
        for key in presses:
            painted.clear()
            await pilot.press(key)
            await pilot.pause(0.1)
            assert painted, key
            assert all(view.y <= box.y and box.bottom <= view.bottom for box in painted), (
                key,
                app.selected,
                painted,
            )


async def test_buttons_carry_no_key_and_tab_reaches_them():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, f"{HOOK}#secret")
        await until(pilot, lambda: bool(app.box(f"{HOOK}#secret").query(ActionButton)))
        labels = {b.label.plain for b in app.query(ActionButton)}
        assert labels == {"Start"}  # no letter, no ⏎
        await pilot.press("tab")
        assert in_box(app) and app.focused.label.plain == "Start"


async def test_a_click_selects_a_box_and_opens_it():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        rid = f"{SEAL}#seal-key"
        await pilot.click(app.box(rid), offset=(5, 1))
        await until(pilot, lambda: in_box(app))  # a click opens the box, as ⏎ does
        assert app.selected == rid and app.focused.label.plain == "Done"  # an external key's


async def test_help_opens_on_question_mark_and_f1_and_lists_no_simulation_key():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        for key in ("question_mark", "f1"):
            await pilot.press(key)
            assert isinstance(app.screen, HelpModal)
            await pilot.press("down")  # the list stays put under a dialog
            assert app.selected == FAILED
            await pilot.press("escape")
            assert not isinstance(app.screen, HelpModal)
        table = HelpModal.table().plain
        assert "Esc" in table and "mock" not in table.lower() and "^T" not in table


def test_the_keys_are_section_7_6_s_and_no_simulation_key_ships():
    """The mock's + and -, ^T and r drive its simulation, and its f failed the next tool step
    (A3); here f is the filter (R89)."""
    keys = {key for binding in RotatorApp.BINDINGS for key in binding.key.split(",")}
    assert keys == {"up", "down", "home", "end", "enter", "escape", "f", "question_mark", "f1"} | {
        "q",
        "ctrl+q",
    }
    assert ("filter", "f") in {(b.action, b.key) for b in RotatorApp.BINDINGS}
    assert "f" in {key for key, _, _ in HELP_ROWS}
    assert not RotatorApp.ENABLE_COMMAND_PALETTE


async def test_q_quits():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        assert app.return_code is None
        await pilot.press("q")
        assert app.return_code == 0


async def test_ctrl_q_quits_with_a_button_focused():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, f"{PAT}#token")
        await pilot.press("enter")
        await until(pilot, lambda: in_box(app))
        await pilot.press("ctrl+q")
        assert app.return_code == 0


# --- the empty state ---------------------------------------------------------------------


async def test_when_nothing_waits_a_green_box_tops_the_list_without_a_next_line():
    bao = fake_of(store())
    yesterday = "2026-10-07"
    for path, key in [
        (PAT, "token"),
        (BOT, "telegram-bot-token"),
        (HOOK, "secret"),
        (IAC_AGENT, "secret_id"),
        (OPENBAO_ADMIN, "secret_id"),
        (SEAL, "seal-key"),
    ]:
        put_state(bao, path, stamps={key: yesterday})
    put_state(bao, MIXED, stamps={"password": yesterday, "pin": yesterday})
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        first, *rest = app.box_list.children
        assert isinstance(first, EmptyState)
        assert first.content.plain == "Nothing waits on you."
        assert [box.item.id for box in rest] == app.order and len(rest) == 10
        assert app.selected == f"{HOOK}#secret"  # the soonest
        status = app.query_one("#status", StatusBar).content.plain
        assert "0 waiting" in status


async def test_with_nothing_listed_the_green_box_is_the_list_s_only_box():
    app = app_of(fake_of(store()), rotations=[])
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        assert [type(w) for w in app.box_list.children] == [EmptyState]
        await pilot.press("down", "end", "home", "enter", "escape", "f")
        assert app.selected is None and app.focused is None and app.filtered is None
        assert "f filter · 0 items" in app.query_one("#footer", Static).content.plain
