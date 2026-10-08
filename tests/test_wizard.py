"""`secret-rotator ui`'s wizard (design §7.4, §7.5), start to done: the app run headless, each
plan run by the real executor against the fake OpenBao, its tool steps the test double's. The
screens collated from the plan, the button bar and its progress, the credential, show and confirm
screens, the expiry, the live step log, Copy, the question dialog, done, the lock across the UI and
a leaf's one plan in flight, and quitting."""

from fixtures import compliant_store, edit, fields_of
from plans import LEAF, client, fake, fake_of, flight_of, put_flight, put_state, state_of
from sim import Cue, Held, vendor
from test_approle import roles
from test_listing import (
    BOT,
    HOOK,
    IAC_AGENT,
    MIXED,
    OPENBAO_ADMIN,
    PAT,
    SEAL,
    WIFI,
    YEARLY,
    leaf,
    store,
)
from test_ui import FAILED, IN_FLIGHT, SIZE, app_of, in_box, select, settled, until, world
from test_ui import tones as ui_tones
from textual.widgets import Input, Static

from secret_rotator.contract import LOCK_LEAF, MARKER_VALUE
from secret_rotator.kinds.external import RUNBOOK
from secret_rotator.kinds.manual import TYPES
from secret_rotator.lock import Lock
from secret_rotator.model import Action
from secret_rotator.ui.app import BUSY, BUSY_DONE, BUSY_OF, QUIT, QUIT_RUN, WAITS
from secret_rotator.ui.item import Item, Line, LineState, Phase
from secret_rotator.ui.widgets import (
    ActionButton,
    ButtonBar,
    ConfirmModal,
    CredentialField,
    EmptyState,
    ExpiryField,
    Instruction,
    Notice,
    SecretTextArea,
    StatusBar,
    StepLog,
    ValueBox,
)

TOKEN = "vpat_1234567890abcdef"
VENDOR = f"{LEAF}#token"
HOOK_ID = f"{HOOK}#secret"
PAT_ID = f"{PAT}#token"
SEAL_ID = f"{SEAL}#seal-key"
WIFI_ID = f"{WIFI}#password"


def tones(box):
    return ui_tones(box) | {t for t in ("primary", "success") if box.has_class(f"-{t}")}


def phase(app, rid):
    return app.items[rid].phase


def specs(app, rid):
    return {spec.action: spec for spec in app.buttons(app.box(rid))}


def labels(app, rid):
    return [b.label.plain for b in app.box(rid).query(ActionButton)]


def status_of(widget) -> str:
    return widget.query_one(".field-status", Static).content.plain


def expires_at(bao, leaf, key="token"):
    return fields_of(bao.meta(leaf), key).get("expires_at")


async def go(pilot, app):
    """⏎ until the selected box has focus (⏎ in the list opens it), then ⏎ on its first field or
    button."""
    if not in_box(app):
        await pilot.press("enter")
        await until(pilot, lambda: in_box(app))
    await pilot.press("enter")


async def submit(pilot, app):
    """⏎ in a filled field goes to Continue; ⏎ there presses it."""
    await pilot.press("enter")
    await until(
        pilot,
        lambda: isinstance(app.focused, ActionButton) and app.focused.label.plain == "Continue",
    )
    await pilot.press("enter")


async def at_credential(pilot, app, rid):
    await select(pilot, app, rid)
    await go(pilot, app)
    await until(pilot, lambda: phase(app, rid) is Phase.WAITING and isinstance(app.focused, Input))


def vendor_app(*held):
    bao = fake()
    return bao, app_of(bao, rotations=[vendor(*held)])


# --- start to done ----------------------------------------------------------------


async def test_a_plan_runs_start_to_finish_and_its_box_leaves_the_list():
    """You · tool · you: the credential, the tool's steps live, the confirm the stamp rides on;
    then `done` and the box leaves."""
    cue = Cue()
    bao, app = vendor_app(Held("rollout", "roll out app/deployment/app", cue=cue))
    app.linger = 1.0  # long enough to see `done`
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        screens = app.items[VENDOR].screens
        assert [s.actor.value for s in screens] == ["operator", "tool", "operator"]
        await pilot.press(*TOKEN)
        await submit(pilot, app)
        await until(pilot, lambda: cue.holding.is_set() and phase(app, VENDOR) is Phase.RUNNING)
        await until(pilot, lambda: bool(app.box(VENDOR).query(StepLog)))
        assert app.focused is None  # nothing takes focus while the screen's steps run
        log = app.box(VENDOR).query_one(StepLog)
        await until(pilot, lambda: len(log.lines) == 3)
        rows = [row.plain for row in log.rows(100)]
        assert rows[0].startswith("✓ write eso/prd/app/prd/token")
        assert rows[1].startswith("✓ copy")
        assert rows[2][0] in "◐◓◑◒" and "roll out app/deployment/app · 1/2 Ready" in rows[2]
        cue.go()
        await until(pilot, lambda: phase(app, VENDOR) is Phase.WAITING and in_box(app))
        assert app.focused.label.plain == "Done"  # the new screen's first button
        assert app.box(VENDOR).query_one(Instruction).content.plain.startswith("Revoke the old")
        await pilot.press("enter")
        await until(pilot, lambda: phase(app, VENDOR) is Phase.DONE)
        box = app.box(VENDOR)
        assert str(box.border_title).startswith("✓ ") and tones(box) == {"success"}
        assert box.query_one(".info", Static).content.plain == "done"
        assert box.query_one(".progress", Static).content.plain.startswith("Step 3 of 3")
        assert box.query_one(".progress", Static).content.plain.endswith("done")
        await until(pilot, lambda: VENDOR not in app.order)
        assert app.done_count == 1
    assert bao.data(LEAF)["token"] == TOKEN and bao.data("iac/copy")["token"] == TOKEN
    assert state_of(bao, LEAF).stamps == {"token": "2026-10-08"}
    assert flight_of(bao, LEAF) is None and bao.data(LOCK_LEAF) == {}


async def test_a_done_box_leaves_and_the_one_below_is_selected_the_one_above_when_last():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, HOOK_ID)
        below = app.order[app.order.index(HOOK_ID) + 1]
        await go(pilot, app)  # Start: its tool screen runs by itself, to its confirm
        await until(pilot, lambda: phase(app, HOOK_ID) is Phase.WAITING and in_box(app))
        await pilot.press("enter")  # Done
        await until(pilot, lambda: HOOK_ID not in app.order)
        assert app.selected == below and app.focused is None
        assert app.order[-1] == WIFI_ID
        above = app.order[-2]
        await at_credential(pilot, app, WIFI_ID)
        await pilot.press(*"a new psk")
        await submit(pilot, app)
        await until(pilot, lambda: WIFI_ID not in app.order)
        assert app.selected == above
        status = app.query_one("#status", StatusBar).content.plain
        assert status.endswith("2 done this session")


async def test_when_the_done_box_was_the_last_that_waits_the_green_box_tops_the_list():
    bao = fake_of(store())
    for path, key in [(PAT, "token"), (BOT, "telegram-bot-token"), (IAC_AGENT, "secret_id")]:
        put_state(bao, path, stamps={key: "2026-10-07"})
    for path, key in [(OPENBAO_ADMIN, "secret_id"), (SEAL, "seal-key")]:
        put_state(bao, path, stamps={key: "2026-10-07"})
    put_state(bao, MIXED, stamps={"password": "2026-10-07", "pin": "2026-10-07"})
    app = app_of(bao)  # only the hook is due
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        assert not app.query(EmptyState) and app.selected == HOOK_ID and app.waiting() == 1
        await go(pilot, app)
        await until(pilot, lambda: phase(app, HOOK_ID) is Phase.WAITING and in_box(app))
        await pilot.press("enter")
        await until(pilot, lambda: bool(app.query(EmptyState)))
        assert isinstance(app.box_list.children[0], EmptyState)
        status = app.query_one("#status", StatusBar).content.plain
        assert "0 waiting · 1 done this session" in status


async def test_an_external_box_shows_its_notes_and_the_runbook_and_done_only_stamps_it():
    bao = world()
    before = bao.data(SEAL)
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, SEAL_ID)
        box = app.box(SEAL_ID)
        await until(pilot, lambda: bool(box.query(ActionButton)))
        text = box.query_one(Instruction).content.plain
        step = app.items[SEAL_ID].rotation.plan.steps[0]
        assert text == f"{step.title}\n{step.instruction}"
        assert text.startswith("Rotate seal-key outside the tool") and RUNBOOK in text
        assert labels(app, SEAL_ID) == ["Done"]  # no Start, no wizard
        await go(pilot, app)
        await until(pilot, lambda: SEAL_ID not in app.order)
    assert bao.data(SEAL) == before
    assert state_of(bao, SEAL).stamps["seal-key"] == "2026-10-08"


# --- the credential screen ------------------------------------------------------------


async def test_the_field_is_a_normal_input_and_reveal_and_clear_work_on_it():
    bao, app = vendor_app()
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*"vpat_1234X", "backspace", "5")
        await pilot.press("home", "delete", "w")  # arrows and editing, as in any input
        assert app.drafts[VENDOR]["token"] == "wpat_12345"
        assert app.focused.password
        assert labels(app, VENDOR) == ["Continue", "Reveal", "Clear", "Abort"]
        app.action_press_button("reveal")
        await pilot.pause(0.05)
        assert app.box(VENDOR).revealed and not app.focused.password
        assert labels(app, VENDOR)[1] == "Hide"
        assert specs(app, VENDOR)["continue"].enabled
        app.action_press_button("clear")
        await pilot.pause(0.05)
        assert app.drafts[VENDOR]["token"] == "" and app.focused.value == ""
        assert not specs(app, VENDOR)["continue"].enabled


async def test_a_field_checks_its_value_on_the_line_under_it():
    bao, app = vendor_app()
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        field = app.box(VENDOR).query_one(CredentialField)
        assert status_of(field) == "empty"
        await pilot.press(*TOKEN)
        await pilot.pause(0.05)
        assert status_of(field) == f"{len(TOKEN)} chars  ✓ starts with vpat_"
        field.set_value("ghp_other")
        await pilot.pause(0.05)
        assert status_of(field) == "9 chars  ⚠ expected: starts with vpat_"


async def test_a_value_off_its_shape_asks_before_continuing():
    bao, app = vendor_app()
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*"ghp_not_a_vendor_token")
        await submit(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert app.screen.question == "The token does not look as expected. Continue anyway?"
        assert app.screen.detail == "token: expected starts with vpat_"
        await pilot.press("enter")  # No is focused: a stray ⏎ does not continue
        await until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        assert phase(app, VENDOR) is Phase.WAITING and app.items[VENDOR].screen.index == 0
        await until(
            pilot,
            lambda: isinstance(app.focused, ActionButton) and app.focused.label.plain == "Continue",
        )
        await until(pilot, lambda: not app.focused.has_class("-active"))  # 0.2 s after a press
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await until(pilot, lambda: app.items[VENDOR].screen.index >= 1)
    assert bao.data(LEAF)["token"] == "ghp_not_a_vendor_token"


async def test_the_question_s_buttons_take_the_arrows_and_leave_the_list_alone():
    bao, app = vendor_app()
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*"off")
        await submit(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert app.focused.id == "no"
        await pilot.press("left")
        assert app.focused.id == "yes"
        await pilot.press("down", "right")
        assert app.focused.id == "yes" and app.selected == VENDOR
        await pilot.press("right")
        assert app.focused.id == "no"
        await pilot.press("q")  # no second question, and no quit
        assert isinstance(app.screen, ConfirmModal) and app.return_code is None
        await pilot.press("enter")
        await until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        assert phase(app, VENDOR) is Phase.WAITING


async def test_enter_in_a_field_goes_to_the_continue_button_which_carries_no_key():
    bao, app = vendor_app()
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        assert all("⏎" not in b.label.plain for b in app.query(ActionButton))
        await pilot.press("enter")  # empty: it stays in the field
        assert isinstance(app.focused, Input) and app.focused.border_title == "token"
        await pilot.press(*TOKEN, "enter")
        await until(pilot, lambda: isinstance(app.focused, ActionButton))
        assert app.focused.label.plain == "Continue" and app.items[VENDOR].screen.index == 0


async def test_a_new_wizard_screen_focuses_its_first_field_or_button():
    """A failure's new buttons on the same screen: test_wizard_failure."""
    bao, app = vendor_app()
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, VENDOR)
        await settled(pilot)
        assert app.focused is None  # selecting does not open
        await go(pilot, app)  # Start
        await until(pilot, lambda: isinstance(app.focused, Input))  # the credential's field
        await pilot.press(*TOKEN)
        await submit(pilot, app)
        await until(pilot, lambda: app.items[VENDOR].screen.index == 2 and in_box(app))
        assert app.focused.label.plain == "Done"  # the confirm screen's first button


# --- the expiry -------------------------------------------------------------------


async def test_the_expiry_is_today_plus_the_interval_and_enter_accepts_it():
    """A GitHub personal access token's, yearly."""
    bao = world()
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, PAT_ID)
        expiry = app.box(PAT_ID).query_one(ExpiryField)
        assert expiry.editor.value == "2027-10-08"  # today plus its 365 days
        assert expiry.editor.border_title == "expires" and status_of(expiry) == "✓ in 365 days"
        await pilot.press(*"github_pat_11ABC")
        await submit(pilot, app)  # the expiry is no empty field: ⏎ goes to Continue
        await until(pilot, lambda: PAT_ID not in app.order)
    assert bao.data(PAT)["token"] == "github_pat_11ABC"
    assert state_of(bao, PAT).stamps["token"] == "2026-10-08"
    assert expires_at(bao, PAT) == "2027-10-08"


async def test_a_cleared_expiry_says_the_credential_never_expires_and_is_taken():
    bao, app = vendor_app()
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*TOKEN, "tab")
        assert app.focused.border_title == "expires"
        app.focused.value = ""
        await pilot.pause(0.05)
        assert status_of(app.box(VENDOR).query_one(ExpiryField)) == "never expires"
        assert specs(app, VENDOR)["continue"].enabled  # it waits for the credential only
        await submit(pilot, app)
        await until(pilot, lambda: app.items[VENDOR].screen.index == 2 and in_box(app))
        await pilot.press("enter")
        await until(pilot, lambda: phase(app, VENDOR) is Phase.DONE)
    assert expires_at(bao, LEAF) is None


async def test_an_expiry_that_is_no_date_after_today_says_why_and_never_reaches_the_step():
    bao, app = vendor_app()
    bells = []
    app.bell = lambda: bells.append(1)
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*TOKEN)
        expiry = app.box(VENDOR).query_one(ExpiryField)
        for text, why in [
            ("2026-10-08", "⚠ 2026-10-08 is not after today, 2026-10-08"),
            ("next year", "⚠ 'next year' is not an ISO date (YYYY-MM-DD)"),
        ]:
            expiry.editor.value = text
            await pilot.pause(0.05)
            assert status_of(expiry) == why
            app.action_press_button("continue")
            await pilot.pause(0.05)
            assert app.focused is expiry.editor and phase(app, VENDOR) is Phase.WAITING
        assert len(bells) == 2 and flight_of(bao, LEAF).step == "operator.credential:token"


async def test_a_key_without_an_interval_starts_its_expiry_blank():
    compliant = compliant_store()
    edit(compliant[LEAF].meta, "token", interval="never", notes="by hand")
    app = app_of(fake(), rotations=[vendor(store=compliant)])
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        expiry = app.box(VENDOR).query_one(ExpiryField)
        assert expiry.editor.value == "" and status_of(expiry) == "never expires"


async def test_a_credential_that_does_not_expire_has_no_expiry_field():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, WIFI_ID)
        assert not app.box(WIFI_ID).query(ExpiryField)


# --- the show screen ------------------------------------------------------------------


def approle_world():
    """world() with the AppRoles approle's plans mint from, the manual deliveries' leaves holding
    the marker text."""
    bao = world()
    bao.approles = roles()
    bao.leaves[IAC_AGENT]["data"] = {"secret_id": MARKER_VALUE}
    return bao


async def test_a_show_screen_masks_the_value_until_reveal_and_copies_it():
    """iac-agent's secret_id, delivered by hand: minted, shown, then written and the old one
    destroyed."""
    bao = approle_world()
    app = app_of(bao)
    copied = []
    app.copy_to_clipboard = copied.append
    rid = f"{IAC_AGENT}#secret_id"
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, rid)
        await go(pilot, app)
        await until(pilot, lambda: phase(app, rid) is Phase.WAITING and in_box(app))
        value = app.items[rid].request.value
        box = app.box(rid)
        shown = box.area.of(ValueBox)[0]
        assert shown.border_title == "secret_id"
        assert shown.content.plain == "•" * len(value)
        assert labels(app, rid) == ["Done", "Reveal", "Copy", "Abort"]
        assert app.focused.label.plain == "Done"
        app.action_press_button("reveal")
        await pilot.pause(0.05)
        assert shown.content.plain == value and labels(app, rid)[1] == "Hide"
        app.action_press_button("copy")
        await pilot.pause(0.05)
        assert copied == [value] and labels(app, rid)[2] == "✓ Copied"
        await until(pilot, lambda: labels(app, rid)[2] == "Copy", timeout=5)
        await pilot.press("enter")  # Done
        await until(pilot, lambda: rid not in app.order)
    assert value in bao.live("iac-agent")


# --- the progress, the colours and the step log ----------------------------------------


async def test_the_bar_shows_the_screen_s_progress_and_the_box_takes_the_colour_of_whose_move():
    cue = Cue()
    held = Held("rollout", "roll out app/deployment/app", cue=cue, estimate=100)
    bao, app = vendor_app(held)
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        box = app.box(VENDOR)

        def progress():
            return box.query_one(".progress", Static).content.plain

        assert progress().startswith("Step 1 of 3") and progress().endswith("~2 min left")
        assert tones(box) == {"accent"} and str(box.border_title).startswith("● ")  # your move
        info = box.query_one(".info", Static).content.plain
        assert info == "overdue 37d · paste a new vendor token · ~2 min"  # the plan's estimate
        await pilot.press(*TOKEN)
        await submit(pilot, app)
        await until(pilot, lambda: cue.holding.is_set() and tones(box) == {"primary"})
        assert progress().startswith("Step 2 of 3") and progress().endswith("~2 min left")
        assert "[" not in str(box.border_title).split("]")[-1]  # one colour: the glyph unstyled
        assert box.query_one(".info", Static).content.plain == info
        cue.go()
        await until(pilot, lambda: phase(app, VENDOR) is Phase.WAITING and tones(box) == {"accent"})
        assert progress().startswith("Step 3 of 3") and progress().endswith("<1 min left")


async def test_a_log_shows_at_most_eight_lines_the_earliest_folded():
    cue = Cue()
    syncs = [Held(f"eso.sync:app/s{n}", f"sync ExternalSecret app/s{n}") for n in range(8)]
    bao, app = vendor_app(*syncs, Held("rollout", "roll out app/deployment/app", cue=cue))
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*TOKEN)
        await submit(pilot, app)
        await until(pilot, lambda: cue.holding.is_set() and bool(app.box(VENDOR).area.of(StepLog)))
        log = app.box(VENDOR).area.of(StepLog)[0]
        await until(pilot, lambda: len(log.lines) == 11)  # write, copy, 8 syncs, the rollout
        rows = [row.plain for row in log.rows(100)]
        assert len(rows) == 8 and rows[0].startswith("✓ 4 earlier steps")
        assert rows[1].startswith("✓ sync ExternalSecret app/s2") and "roll out" in rows[-1]
        cue.go()
        await until(pilot, lambda: phase(app, VENDOR) is Phase.WAITING)


async def test_an_operator_screen_logs_no_line_of_its_own_step():
    """The log is the tool's (§7.4): the credential and confirm screens show none, typing
    included."""
    cue = Cue()
    bao, app = vendor_app(Held("rollout", "roll out app/deployment/app", cue=cue))
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*"vpat_1")
        await pilot.pause(0.05)
        assert [log.lines for log in app.box(VENDOR).area.of(StepLog)] == [[]]
        await pilot.press(*TOKEN[6:])
        await submit(pilot, app)
        await until(pilot, lambda: cue.holding.is_set())
        cue.go()
        await until(pilot, lambda: phase(app, VENDOR) is Phase.WAITING and in_box(app))
        assert [log.lines for log in app.box(VENDOR).area.of(StepLog)] == [[]]


def test_an_answered_operator_screen_shows_only_a_failed_silent_step_s_line():
    item = Item.of(vendor())
    item.at = 3  # the confirm, answered: its stamp runs
    revoke, stamp = item.rotation.plan.steps[3:]
    item.lines[(revoke.id, Action.RUN)] = Line(revoke, Action.RUN, 0.0, LineState.OK)
    item.lines[(stamp.id, Action.RUN)] = Line(stamp, Action.RUN, 0.0)
    assert item.visible() == []
    item.lines[(stamp.id, Action.RUN)].state = LineState.FAILED
    assert [line.step for line in item.visible()] == [stamp]


def test_a_failed_line_has_its_error_under_it_on_top_of_the_eight():
    steps = [Held(f"s{n}", f"step {n}") for n in range(9)]
    lines = [Line(step, Action.RUN, 0.0, LineState.OK, elapsed=1.5) for step in steps]
    lines[-1].state, lines[-1].error = LineState.FAILED, "0/2 Ready after 5m0s"
    log = StepLog()
    log.lines = lines
    rows = [row.plain for row in log.rows(80)]
    assert len(rows) == 9 and rows[0].startswith("✓ 2 earlier steps") and rows[0].endswith("3.0s")
    assert rows[-2].startswith("✗ step 8") and rows[-1] == "  0/2 Ready after 5m0s"


# --- one plan at a time -------------------------------------------------------------


async def test_one_plan_at_a_time_every_other_box_says_what_it_cannot_do():
    app = app_of(world())
    bells = []
    app.bell = lambda: bells.append(1)
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, PAT_ID)
        start, done = specs(app, HOOK_ID)["start"], specs(app, SEAL_ID)["done"]
        assert (start.enabled, start.reason) == (False, BUSY)
        assert (done.enabled, done.reason) == (False, BUSY_DONE)
        await select(pilot, app, HOOK_ID)
        await until(pilot, lambda: bool(app.box(HOOK_ID).area.of(ButtonBar)))
        assert app.box(HOOK_ID).query_one(".reason", Static).content.plain == BUSY
        await pilot.press("enter")  # its only button is disabled: it is not opened
        assert bells == [1] and app.focused is None
        app.start(app.items[HOOK_ID])  # a press the button's state did not stop
        assert phase(app, HOOK_ID) is Phase.DUE and bells == [1, 1]
        failed, in_flight = specs(app, FAILED), specs(app, IN_FLIGHT)
        for spec in (failed["retry"], failed["abort"]):
            assert (spec.enabled, spec.reason) == (False, BUSY_OF.format("retry or abort"))
        assert failed["details"].enabled  # reading is not running
        for spec in (in_flight["resume"], in_flight["abort"]):
            assert (spec.enabled, spec.reason) == (False, BUSY_OF.format("resume or abort"))
        app.retry(app.items[FAILED])
        app._abort(app.items[IN_FLIGHT])
        assert bells == [1, 1, 1, 1] and app.active.item.id == PAT_ID
        await select(pilot, app, PAT_ID)  # the wizard, where it is
        assert phase(app, PAT_ID) is Phase.WAITING
        assert app.box(PAT_ID).area.of(CredentialField)


async def test_while_one_plan_of_a_leaf_is_in_flight_its_other_plans_wait():
    app = app_of(fake_of(store()))  # nothing in flight
    password, pin = f"{MIXED}#password", f"{MIXED}#pin"
    async with app.run_test(size=SIZE) as pilot:
        assert not app.waits(app.items[pin])
        await at_credential(pilot, app, password)
        assert app.waits(app.items[pin])
        await pilot.press(*"a new password")
        await submit(pilot, app)
        await until(pilot, lambda: password not in app.order)
        assert not app.waits(app.items[pin]) and specs(app, pin)["start"].enabled


async def test_a_plan_in_flight_that_the_list_does_not_show_keeps_the_leaf_s_others_waiting():
    bao = fake_of(store())
    put_flight(bao, "random", MIXED, ["token"], "kv.write")  # no operator step: not listed
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        for rid in (f"{MIXED}#password", f"{MIXED}#pin"):
            start = specs(app, rid)["start"]
            assert (start.enabled, start.reason) == (False, WAITS)


# --- the lock ---------------------------------------------------------------------


async def test_a_start_that_finds_a_dead_holder_s_lock_asks_to_break_it():
    bao = world()
    Lock(client(bao), "run x on gone, pid 1").take("a plan")
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, HOOK_ID)
        await go(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert app.screen.question == "Is run x on gone, pid 1 gone? Break its lock?"
        assert app.screen.detail.startswith("Another plan runs: run x on gone, pid 1 holds it")
        await pilot.press("n")
        await until(pilot, lambda: phase(app, HOOK_ID) is Phase.DUE and app.active is None)
        await settled(pilot)
        (notice,) = app.box(HOOK_ID).area.of(Notice)
        assert notice.display and notice.content.plain.startswith("Another plan runs:")
        assert bao.data(LOCK_LEAF)["holder"] == "run x on gone, pid 1"
        await go(pilot, app)  # Start again, and break it
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await until(pilot, lambda: phase(app, HOOK_ID) is Phase.WAITING)
        assert app.items[HOOK_ID].said[-1] == "The lock is broken."
        assert bao.data(LOCK_LEAF)["holder"] == "secret-rotator ui"  # it spans the wizard


# --- quitting -----------------------------------------------------------------------


async def test_quitting_at_an_operator_step_leaves_the_plan_in_flight_there_with_the_lock_free():
    bao = world()
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, HOOK_ID)
        await go(pilot, app)
        await until(pilot, lambda: phase(app, HOOK_ID) is Phase.WAITING)
        confirm = app.items[HOOK_ID].rotation.plan.steps[app.items[HOOK_ID].at].id
        await pilot.press("q")
        await until(pilot, lambda: app.return_code == 0)
    assert flight_of(bao, HOOK).step == confirm and confirm.startswith("operator.confirm:")
    assert bao.data(LOCK_LEAF) == {}


async def test_quitting_while_a_tool_step_runs_asks_then_leaves_the_plan_in_flight_there():
    cue = Cue()
    bao, app = vendor_app(Held("rollout", "roll out app/deployment/app", cue=cue))
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*TOKEN)
        await submit(pilot, app)
        await until(pilot, lambda: cue.holding.is_set())
        await pilot.press("q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert (app.screen.question, app.screen.detail) == (QUIT, QUIT_RUN)
        await pilot.press("n")
        await until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        assert phase(app, VENDOR) is Phase.RUNNING and cue.holding.is_set()
        assert app.return_code is None
        await pilot.press("ctrl+q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await until(pilot, lambda: app.return_code == 0)
    assert flight_of(bao, LEAF).step == "rollout" and bao.data(LOCK_LEAF) == {}


# --- a value that spans lines ---------------------------------------------------------


async def test_a_value_that_spans_lines_is_a_multi_line_field_drawn_as_dots():
    path = "eso/prd/vpn/prd/wireguard"
    entry = {"kind": "manual", "args": {"type": "torguard-wireguard"}, **YEARLY}
    app = app_of(fake_of({path: leaf(path, config=entry)}))
    rid = f"{path}#config"
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, rid)
        await go(pilot, app)
        await until(pilot, lambda: isinstance(app.focused, SecretTextArea))
        await pilot.press(*"[Interface]", "enter", *"PrivateKey=k", "enter", *"[Peer]")
        (field,) = app.box(rid).area.of(CredentialField)
        assert field.value == "[Interface]\nPrivateKey=k\n[Peer]"  # ⏎ is a newline in it
        words = TYPES["torguard-wireguard"].shape.words
        assert status_of(field) == f"3 lines · 31 chars  ✓ {words}"
        assert app.focused.get_line(1).plain == "•" * len("PrivateKey=k")
        await pilot.press("tab")  # Tab reaches Continue
        assert app.focused.label.plain == "Continue"
