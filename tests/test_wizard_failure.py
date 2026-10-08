"""`secret-rotator ui`'s wizard when a plan does not go straight to done (design §4.5, §7.3-§7.5): a
failure with Retry · Abort · Details, told in Telegram; Abort behind its guard — a cancel, a
rollback on its own screen, refused after a step without an undo, a running tool step stopped; a
failed rollback; a plan left in flight, resumed or aborted; an earlier session's plan; quitting
while a tool step runs. The app runs headless, each plan run by the real executor against the fake
OpenBao, its tool steps the test double's, which fail on cues from test code (A3)."""

from plans import LEAF, fake, flight_of, put_flight, put_state, state_of
from sim import REVOKED, Cue, Held, again
from sim import vendor as vendor_rotation
from test_listing import PAT, SEAL
from test_ui import FAILED, SIZE, app_of, in_box, info, select, settled, until, world
from test_wizard import (
    TOKEN,
    VENDOR,
    at_credential,
    go,
    labels,
    phase,
    specs,
    submit,
    tones,
)
from textual.widgets import Input, Static

from secret_rotator.contract import LOCK_LEAF, STATE_LEAF
from secret_rotator.staging import ROLLBACK
from secret_rotator.ui.app import NO_ROLLBACK, QUIT, QUIT_ROLLBACK, ROLLING_BACK
from secret_rotator.ui.item import Phase
from secret_rotator.ui.run import COMMAND
from secret_rotator.ui.widgets import (
    ActionButton,
    ConfirmModal,
    DetailsModal,
    HelpModal,
    Instruction,
    StepLog,
)

ROLLOUT = "roll out app/deployment/app"
SEAL_ID = f"{SEAL}#seal-key"
PAT_ID = f"{PAT}#token"


def vendor_run(*held, notify=None):
    """The fake, the app over the vendor plan around the held steps, and that plan as listed."""
    bao = fake()
    listed = vendor_rotation(*held)
    return bao, app_of(bao, rotations=[listed], notify=notify), listed


def rows(app, rid):
    return [row.plain for row in app.box(rid).area.of(StepLog)[0].rows(100)]


def progress(app, rid):
    return app.box(rid).query_one(".progress", Static).content.plain


def instruction(app, rid):
    return app.box(rid).query_one(Instruction).content.plain


async def to_held(pilot, app, cue):
    """The token entered; the tool's screen runs to the step held on the cue."""
    await at_credential(pilot, app, VENDOR)
    await pilot.press(*TOKEN)
    await submit(pilot, app)
    await until(pilot, lambda: cue.holding.is_set())


async def failed(pilot, app, cue, error="0/2 Ready after 5m0s"):
    await to_held(pilot, app, cue)
    cue.fail(error)
    await until(pilot, lambda: phase(app, VENDOR) is Phase.FAILED and in_box(app))


async def abort(pilot, app):
    """Abort is a button with no key of its own: focus it, press it; its question opens."""

    def button():
        box = app.box(app.selected)
        found = [b for b in box.area.of(ActionButton) if b.is_mounted and b.label.plain == "Abort"]
        return found[0] if found and not found[0].disabled else None

    await until(pilot, lambda: button() is not None)
    button().focus()
    await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, ConfirmModal))


async def left_at_the_revoke():
    """A vendor plan left in flight at its revoke confirm by a quit, and the next app over it."""
    bao, app, listed = vendor_run()
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*TOKEN)
        await submit(pilot, app)
        await until(pilot, lambda: app.items[VENDOR].screen.index == 2 and in_box(app))
        await pilot.press("q")  # at an operator step: no question
        await until(pilot, lambda: app.return_code == 0)
    return bao, app_of(bao, rotations=[again(bao, listed)])


# --- failure, Retry, Details ----------------------------------------------------------------


async def test_a_failure_shows_retry_abort_and_details_is_told_and_retry_counts_its_attempt():
    cue = Cue()
    told = []
    bao, app, _ = vendor_run(Held("rollout", ROLLOUT, cue=cue), notify=told.append)
    async with app.run_test(size=SIZE) as pilot:
        await failed(pilot, app, cue)
        box = app.box(VENDOR)
        assert labels(app, VENDOR) == ["Retry", "Abort", "Details"]
        assert app.focused.label.plain == "Retry"  # the buttons changed: the first takes focus
        assert str(box.border_title).startswith("✗ ") and tones(box) == {"error"}
        line = f"overdue 37d · failed at step 2 of 3 · {ROLLOUT}"
        assert info(app, VENDOR).plain == line
        assert progress(app, VENDOR).startswith("Step 2 of 3")
        assert progress(app, VENDOR).endswith("failed")
        assert rows(app, VENDOR)[-2].startswith(f"✗ {ROLLOUT} · 1/2 Ready")
        assert rows(app, VENDOR)[-1] == "  0/2 Ready after 5m0s"
        name = app.items[VENDOR].rotation.plan.name
        assert told == [
            f"In `{COMMAND}`: The {name} (token) failed at {ROLLOUT}: 0/2 Ready after 5m0s"
        ]
        assert state_of(bao, LEAF).status == "failed"
        await pilot.press("enter")  # Retry
        await until(pilot, lambda: cue.holding.is_set() and phase(app, VENDOR) is Phase.RUNNING)
        assert tones(box) == {"primary"} and labels(app, VENDOR) == ["Abort"]
        await until(pilot, lambda: "· attempt 2" in rows(app, VENDOR)[-1])
        assert ROLLOUT in rows(app, VENDOR)[-1]
        cue.go()
        await until(pilot, lambda: phase(app, VENDOR) is Phase.WAITING and in_box(app))
        assert app.items[VENDOR].screen.index == 2 and app.focused.label.plain == "Done"


async def test_details_shows_the_technical_detail_over_the_box_and_the_arrows_reach_it():
    cue = Cue()
    bao, app, _ = vendor_run(Held("rollout", ROLLOUT, cue=cue))
    async with app.run_test(size=SIZE) as pilot:
        await failed(pilot, app, cue)
        await pilot.press("right", "right")
        assert app.focused.label.plain == "Details"
        await pilot.press("right")
        assert app.focused.label.plain == "Retry"  # around
        await pilot.press("left")
        assert app.focused.label.plain == "Details"
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DetailsModal))
        assert app.screen.title_text == f"Details · {ROLLOUT}"
        assert "StepFailed: 0/2 Ready after 5m0s" in app.screen.text  # the traceback
        assert app.focused.label.plain == "Close"
        await pilot.press("down")  # the list stays put under a dialog
        assert app.selected == VENDOR
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, DetailsModal))
        assert phase(app, VENDOR) is Phase.FAILED


async def test_an_earlier_session_s_failure_shows_its_screen_and_the_run_state_s_error():
    bao = world()
    put_state(bao, FAILED.split("#")[0], last_run="2026-10-07T04:30:00+00:00")
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        assert app.selected == FAILED and app.focused is None
        await pilot.press("enter")  # opens on Retry, which it does not press
        await until(pilot, lambda: in_box(app))
        assert app.focused.label.plain == "Retry" and phase(app, FAILED) is Phase.FAILED
        assert labels(app, FAILED) == ["Retry", "Abort", "Details"]
        (log,) = app.box(FAILED).area.of(StepLog)
        assert log.lines == [] and not log.display  # no earlier log
        await pilot.press("right", "right", "enter")
        await until(pilot, lambda: isinstance(app.screen, DetailsModal))
        assert app.screen.title_text == "Details · write eso/prd/mixed/prd/creds"
        assert app.screen.text == "2026-10-07T04:30:00+00:00: kv.write: HTTP 403"
        await pilot.press("enter")  # Close
        await until(pilot, lambda: not isinstance(app.screen, DetailsModal))
        await pilot.press("escape")  # back to the list
        assert app.focused is None


async def test_a_failed_stamp_shows_on_the_confirm_it_rides_on_with_abort_refused_after_it():
    """The stamp riding on the revoke's Done fails: the confirm's screen, its failed line red,
    and Abort disabled with its reason, since the old token is revoked."""
    told = []
    bao, app, _ = vendor_run(notify=told.append)
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*TOKEN)
        await submit(pilot, app)
        await until(pilot, lambda: app.items[VENDOR].screen.index == 2 and in_box(app))
        assert labels(app, VENDOR) == ["Done", "Abort"]
        assert specs(app, VENDOR)["abort"].enabled  # the old token is not revoked yet
        bao.refuse[("PATCH", f"kv/metadata/{LEAF}")] = 403  # the stamp's write of the expiry
        await pilot.press("enter")  # Done
        await until(pilot, lambda: phase(app, VENDOR) is Phase.FAILED and in_box(app))
        assert app.focused.label.plain == "Retry"
        item = app.items[VENDOR]
        assert app.view(item) == "confirm" and instruction(app, VENDOR).startswith("Revoke the old")
        assert [line.step.id for line in item.visible()] == ["kv.stamp"]
        assert rows(app, VENDOR)[0].startswith("✗ stamp token")
        abort = specs(app, VENDOR)["abort"]
        assert (abort.enabled, abort.reason) == (False, NO_ROLLBACK.format(REVOKED))
        reason = app.box(VENDOR).query_one(".reason", Static).content.plain
        assert reason == NO_ROLLBACK.format(REVOKED)
        assert labels(app, VENDOR) == ["Retry", "Details", "Abort"]  # the disabled one last
        assert len(told) == 1 and "failed at stamp token" in told[0]
        del bao.refuse[("PATCH", f"kv/metadata/{LEAF}")]
        await pilot.press("enter")  # Retry
        await until(pilot, lambda: VENDOR not in app.order)
    assert state_of(bao, LEAF).stamps == {"token": "2026-10-08"}


async def test_an_external_box_s_failed_stamp_shows_on_it_and_abort_cancels_it():
    bao = world()
    bao.refuse[("POST", f"kv/data/{STATE_LEAF}")] = 403  # the stamp's write of the run state
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, SEAL_ID)
        await go(pilot, app)  # Done
        await until(pilot, lambda: phase(app, SEAL_ID) is Phase.FAILED and in_box(app))
        assert labels(app, SEAL_ID) == ["Retry", "Abort", "Details"]
        assert instruction(app, SEAL_ID).startswith("Rotate seal-key outside the tool")
        assert rows(app, SEAL_ID)[0].startswith("✗ stamp seal-key")
        assert progress(app, SEAL_ID) == ""  # an external box never has a progress bar
        await abort(pilot, app)
        assert app.screen.question == "Abort?"  # it changed nothing
        await pilot.press("y")
        await until(pilot, lambda: phase(app, SEAL_ID) is Phase.DUE)
        assert labels(app, SEAL_ID) == ["Done"] and SEAL_ID in app.order
    assert flight_of(bao, SEAL) is None


# --- Abort ----------------------------------------------------------------------------


async def test_abort_asks_once_then_rolls_back_on_its_own_screen_and_the_box_is_due_again():
    """The undos in reverse, then the activators re-run in order; then due, in place and still
    selected (D21)."""
    sync, cue = Cue(), Cue()
    held = [
        Held("eso.sync:app/s", "sync ExternalSecret app/s", cue=sync, activator=True),
        Held("rollout", ROLLOUT, cue=cue),
    ]
    bao, app, _ = vendor_run(*held)
    app.linger = 1.0  # long enough to see `rolled back`
    before, copied = bao.data(LEAF), bao.data("iac/copy")
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*TOKEN)
        await submit(pilot, app)
        await until(pilot, lambda: sync.holding.is_set())
        sync.go()
        await until(pilot, lambda: cue.holding.is_set())
        cue.fail("0/2 Ready after 5m0s")
        await until(pilot, lambda: phase(app, VENDOR) is Phase.FAILED and in_box(app))
        order = list(app.order)
        await abort(pilot, app)
        assert app.screen.question == "Abort and roll back 4 steps?"
        assert app.focused.id == "no"
        await pilot.press("y")
        await until(pilot, lambda: sync.holding.is_set())  # the sync, again
        box = app.box(VENDOR)
        await until(pilot, lambda: len(rows(app, VENDOR)) == 4)
        assert phase(app, VENDOR) is Phase.ROLLING_BACK
        assert str(box.border_title).startswith("● ") and tones(box) == {"primary"}
        assert instruction(app, VENDOR) == ROLLING_BACK and labels(app, VENDOR) == []
        shown = rows(app, VENDOR)
        assert shown[0].startswith(f"✓ undo: {ROLLOUT}")
        assert shown[1].startswith("✓ undo: copy")
        assert shown[2].startswith(f"✓ undo: write {LEAF}")
        assert shown[3][0] in "◐◓◑◒" and "again: sync ExternalSecret app/s" in shown[3]
        assert progress(app, VENDOR).startswith("Undo 4 of 4")
        sync.go()
        await until(pilot, lambda: phase(app, VENDOR) is Phase.ROLLED_BACK)
        assert info(app, VENDOR).plain == "rolled back" and str(box.border_title)[0] == "○"
        assert progress(app, VENDOR).startswith("Undo 4 of 4")
        assert progress(app, VENDOR).endswith("done")
        await until(pilot, lambda: phase(app, VENDOR) is Phase.DUE)
        assert app.order == order and app.selected == VENDOR  # in place, still selected
        await until(pilot, lambda: labels(app, VENDOR) == ["Start"])
        assert app.focused is None  # back to due, the list has the keys
    assert bao.data(LEAF) == before and bao.data("iac/copy") == copied
    assert flight_of(bao, LEAF) is None and bao.data(LOCK_LEAF) == {}


async def test_abort_while_nothing_mutated_is_a_cancel_that_empties_the_fields():
    bao, app, _ = vendor_run()
    async with app.run_test(size=SIZE) as pilot:
        await at_credential(pilot, app, VENDOR)
        await pilot.press(*"vpat_1")
        await abort(pilot, app)
        assert app.screen.question == "Abort?"
        await pilot.press("y")
        await until(pilot, lambda: phase(app, VENDOR) is Phase.DUE and app.active is None)
        assert flight_of(bao, LEAF) is None and bao.data(LOCK_LEAF) == {}
        await settled(pilot)
        await pilot.press("question_mark")  # the keys work after the dialog closed
        assert isinstance(app.screen, HelpModal)
        await pilot.press("escape")
        await go(pilot, app)  # Start again: Abort emptied the field
        await until(pilot, lambda: isinstance(app.focused, Input))
        assert app.focused.value == "" and app.drafts[VENDOR].get("token", "") == ""


async def test_abort_pressed_while_a_tool_step_runs_stops_it_and_its_undo_runs():
    cue = Cue()
    bao, app, _ = vendor_run(Held("rollout", ROLLOUT, cue=cue))
    app.linger = 1.0
    before = bao.data(LEAF)
    async with app.run_test(size=SIZE) as pilot:
        await to_held(pilot, app, cue)
        assert app.focused is None  # nothing takes focus while the steps run
        await abort(pilot, app)
        assert app.screen.question == "Abort and roll back 3 steps?"
        await pilot.press("y")
        await until(pilot, lambda: phase(app, VENDOR) is Phase.ROLLED_BACK)
        assert not cue.holding.is_set()  # stopped at its progress detail
        labelled = [line.step.title for line in app.items[VENDOR].rollback_lines()]
        assert labelled == [
            ROLLOUT,
            "copy to iac/copy#token",
            f"write {LEAF}",
        ]
        await until(pilot, lambda: phase(app, VENDOR) is Phase.DUE)
    assert bao.data(LEAF) == before and flight_of(bao, LEAF) is None


async def test_a_failed_undo_stops_the_rollback_with_retry_and_details_and_retry_goes_on():
    cue, undo = Cue(), Cue()
    told = []
    held = Held("rollout", ROLLOUT, cue=cue, undo_cue=undo)
    bao, app, _ = vendor_run(held, notify=told.append)
    async with app.run_test(size=SIZE) as pilot:
        await failed(pilot, app, cue)
        await abort(pilot, app)
        await pilot.press("y")
        await until(pilot, lambda: undo.holding.is_set())
        await until(pilot, lambda: progress(app, VENDOR).startswith("Undo 1 of 3"))
        assert labels(app, VENDOR) == []  # no buttons while it runs
        undo.fail("the deployment is gone")
        await until(pilot, lambda: phase(app, VENDOR) is Phase.ROLLBACK_FAILED and in_box(app))
        assert labels(app, VENDOR) == ["Retry", "Details"]  # the rollback is an abort already
        assert app.focused.label.plain == "Retry"
        line = f"overdue 37d · rollback failed at undo: {ROLLOUT}"
        assert info(app, VENDOR).plain == line
        assert progress(app, VENDOR).startswith("Undo 1 of 3")
        assert progress(app, VENDOR).endswith("failed")
        assert rows(app, VENDOR)[0].startswith(f"✗ undo: {ROLLOUT}")
        assert rows(app, VENDOR)[1] == "  the deployment is gone"
        name = app.items[VENDOR].rotation.plan.name
        assert told[1] == (
            f"In `{COMMAND}`: The rollback of the {name} (token) failed at undo: {ROLLOUT}: "
            "the deployment is gone"
        )
        await pilot.press("enter")  # Retry: the rollback goes on
        await until(pilot, lambda: undo.holding.is_set())
        assert phase(app, VENDOR) is Phase.ROLLING_BACK
        await until(pilot, lambda: "· attempt 2" in rows(app, VENDOR)[0])
        undo.go()
        await until(pilot, lambda: phase(app, VENDOR) is Phase.DUE)
    assert flight_of(bao, LEAF) is None


# --- quitting while a tool step runs ----------------------------------------------------


async def test_quitting_in_a_rollback_asks_and_the_next_start_shows_it_stopped_at_its_undo():
    cue, undo = Cue(), Cue()
    held = Held("rollout", ROLLOUT, cue=cue, undo_cue=undo)
    bao, app, listed = vendor_run(held)
    async with app.run_test(size=SIZE) as pilot:
        await failed(pilot, app, cue)
        await abort(pilot, app)
        await pilot.press("y")
        await until(pilot, lambda: undo.holding.is_set())
        await pilot.press("ctrl+q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert (app.screen.question, app.screen.detail) == (QUIT, QUIT_ROLLBACK)
        await pilot.press("y")
        await until(pilot, lambda: app.return_code == 0)
    assert flight_of(bao, LEAF).rolling_back and bao.data(LOCK_LEAF) == {}
    app = app_of(bao, rotations=[again(bao, listed)])
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        box = app.box(VENDOR)
        assert phase(app, VENDOR) is Phase.ROLLBACK_FAILED and str(box.border_title)[0] == "✗"
        line = f"overdue 37d · rollback failed at undo: {ROLLOUT}"
        assert info(app, VENDOR).plain == line
        assert labels(app, VENDOR) == ["Retry", "Details"]
        assert instruction(app, VENDOR) == ROLLING_BACK and rows(app, VENDOR) == []
        assert progress(app, VENDOR).startswith("Undo 1 of 3")
        assert progress(app, VENDOR).endswith("failed")
        await go(pilot, app)  # Retry
        await until(pilot, lambda: undo.holding.is_set())
        undo.go()
        await until(pilot, lambda: phase(app, VENDOR) is Phase.DUE)
    assert flight_of(bao, LEAF) is None


async def test_a_rollback_whose_every_undo_ran_shows_failed_and_retry_ends_it():
    """Its staging leaf outlived the last undo."""
    bao = world()
    put_flight(bao, "manual", PAT, ["token"], "kv.write", **{ROLLBACK: "1"})
    app = app_of(bao)
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, PAT_ID)
        assert info(app, PAT_ID).plain == "overdue 37d · rollback failed"
        await go(pilot, app)  # Retry
        await until(pilot, lambda: phase(app, PAT_ID) is Phase.DUE)
    assert flight_of(bao, PAT) is None


# --- in flight ----------------------------------------------------------------------------


async def test_a_plan_left_in_flight_shows_its_screen_with_resume_and_abort_and_resumes_there():
    """A click opens the box on Resume, and a click on Resume presses it."""
    bao, app = await left_at_the_revoke()
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        box = app.box(VENDOR)
        assert phase(app, VENDOR) is Phase.IN_FLIGHT and str(box.border_title)[0] == "◐"
        assert labels(app, VENDOR) == ["Resume", "Abort"]
        assert instruction(app, VENDOR).startswith("Revoke the old token")  # its screen
        await pilot.click(box, offset=(5, 1))
        await until(pilot, lambda: in_box(app))
        assert app.focused.label.plain == "Resume"
        await pilot.click(app.focused)
        await until(pilot, lambda: phase(app, VENDOR) is Phase.WAITING and in_box(app))
        assert app.items[VENDOR].screen.index == 2
        assert labels(app, VENDOR) == ["Done", "Abort"]  # its own buttons, after Resume
        await pilot.press("enter")
        await until(pilot, lambda: VENDOR not in app.order)
    assert state_of(bao, LEAF).stamps == {"token": "2026-10-08"}


async def test_aborting_a_plan_left_in_flight_rolls_it_back():
    bao, app = await left_at_the_revoke()
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        await abort(pilot, app)
        assert app.screen.question == "Abort and roll back 2 steps?"
        await pilot.press("y")
        await until(pilot, lambda: phase(app, VENDOR) is Phase.DUE)
        assert labels(app, VENDOR) == ["Start"]
    assert flight_of(bao, LEAF) is None and bao.data(LEAF)["token"] != TOKEN
