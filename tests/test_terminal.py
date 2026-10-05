"""The terminal front end (design §4.4): `plan <path>` prints a leaf's plans; `run <path>` runs one
with prompts — a hidden credential with its shape question, Reveal, Retry/Abort/Details after a
failure, the rollback, a plan taken up where it stopped, and a dead holder's lock broken. No value
reaches the output but by Reveal."""

import datetime
import io
import os
import pty
import select
import termios
import threading

import pytest
from plans import COPY, LEAF, client, fake_of
from test_kinds import ACTIVATE_NONE, KINDS, SEAL, TRELLO, WIFI, store_of

from secret_rotator import terminal
from secret_rotator.audit import audit
from secret_rotator.console import PASTE_END, PASTE_START, Console, HiddenEntry
from secret_rotator.contract import LOCK_LEAF, MARKER_VALUE
from secret_rotator.executor import in_flight
from secret_rotator.lock import Lock
from secret_rotator.model import Step, StepFailed
from secret_rotator.opsteps import OperatorConfirm
from secret_rotator.plan import make, tool_part

TODAY = datetime.date(2026, 10, 5)


def console(*answers):
    """A console that is no tty, answering from these lines in order, hidden entries included."""
    return Console(io.StringIO("".join(f"{a}\n" for a in answers)), io.StringIO())


def output(con):
    return con.stdout.getvalue()


def run(bao, leaf, *answers, kinds=KINDS):
    con = console(*answers)
    code = terminal.run_leaf(client(bao), leaf, kinds, con, holder="run test", today=TODAY)
    return code, output(con)


class Flaky(Step):
    """A mutating tool step that fails its first `fail` runs and its first `undo_fail` undos."""

    type = "test.flaky"
    mutates = True

    def __init__(self, fail=1, *, undoable=True, undo_fail=0):
        super().__init__("test.flaky", "do the flaky thing")
        self.fail = fail
        self.undo_fail = undo_fail
        if not undoable:
            self.undo = None
            self.no_undo = "the flaky thing cannot be taken back"

    def run(self, ctx):
        if self.fail:
            self.fail -= 1
            raise StepFailed("the flaky thing failed", "TECHNICAL DETAIL")
        return "flaked"

    def undo(self, ctx):
        if self.undo_fail:
            self.undo_fail -= 1
            raise StepFailed("the undo of the flaky thing failed")
        return "unflaked"


CHECK = OperatorConfirm("check", "check it")


class Wrapped:
    """A kind that adds steps after random's plan; the name stays random's."""

    def __init__(self, *extra, silent_only=False):
        self.inner = KINDS["random"]
        self.extra = extra
        self.silent_only = silent_only
        self.name, self.per_key = "random", False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def plan(self, leaf, ctx):
        if self.silent_only:
            return [s for key in leaf.keys for s in ctx.steps.generate(key)]
        return [*self.inner.plan(leaf, ctx), *self.extra]


class Showing(Wrapped):
    """random's plan with an operator.show of the generated value after the write."""

    def plan(self, leaf, ctx):
        steps = self.inner.plan(leaf, ctx)
        show = ctx.steps.show("value:token", "Store it in RoboForm", "RoboForm: the app token")
        return [*steps, *show]


def kinds_with(kind):
    return {**KINDS, "random": kind}


class TestPlanCommand:
    def test_it_prints_each_plan_with_every_step_and_target_and_why_other_keys_have_none(self):
        store = store_of(**ACTIVATE_NONE, iac__copy="manual:tell the copy's reader")
        lines = []
        assert terminal.print_leaf(lines.append, LEAF, store, audit(store), KINDS, TODAY) == 0
        assert lines == [
            LEAF,
            "  random plan of token · due: never rotated · tell the copy's reader",
            "      The tool generates a new 43-character token and writes it to the leaf and its "
            "1 copy and activates what reads it. You confirm what only you can do.",
            "      1  tool  random.generate       generate a new token  (silent)",
            f"      2  tool  kv.write              write {LEAF}",
            f"      3  tool  kv.copy               copy to {COPY}#token",
            "      4  you   operator.confirm      tell the copy's reader",
            "      5  tool  kv.stamp              stamp token  (silent)",
        ]

    def test_a_plan_that_cannot_be_built_says_why_and_fails_the_command(self):
        store = store_of()
        lines = []
        assert terminal.print_leaf(lines.append, LEAF, store, audit(store), KINDS, TODAY) == 1
        assert (
            lines[2] == f"      cannot be built: {LEAF}: its activation is read from the cluster, "
            "which an offline plan does not reach"
        )

    def test_the_plan_in_flight_and_the_keys_without_a_plan_are_named(self):
        store = store_of(**ACTIVATE_NONE)
        store[TRELLO].meta["rotator_step"] = "manual/token/kv.write"
        lines = []
        terminal.print_leaf(lines.append, TRELLO, store, audit(store), KINDS, TODAY)
        assert lines[1] == "  in flight: its manual plan of token, at kv.write"
        assert (
            "  manual plan of api-key · never due: rotated by hand only · paste a new api-key"
            in lines
        )

    def test_no_leaf_is_an_error(self):
        store = store_of()
        lines = []
        assert terminal.print_leaf(lines.append, "no/leaf", store, audit(store), KINDS, TODAY) == 1
        assert lines == ["error: no leaf no/leaf"]


class TestRun:
    def test_it_shows_the_plan_and_runs_it_once_started(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        old = bao.data(LEAF)["token"]
        code, out = run(bao, LEAF, "y")
        assert code == 0
        new = bao.data(LEAF)["token"]
        assert new != old and bao.data(COPY) == {"token": new}
        assert f"✓ write {LEAF} · v1 → v2" in out
        assert "Done: token of eso/prd/app/prd/token rotated." in out
        assert new not in out and old not in out
        assert "random.generate" in out  # the plan was shown before it started

    def test_declined_it_changes_nothing(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        assert run(bao, LEAF, "n")[0] == 0
        assert bao.writes() == []

    def test_the_operator_picks_one_of_several_plans(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        code, out = run(bao, TRELLO, "3", "y", "NEW-token", "c")
        assert code == 0
        assert "  1  random plan of bearer-token" in out and "  3  manual plan of token" in out
        assert bao.data(TRELLO)["token"] == "NEW-token" and "NEW-token" not in out
        assert bao.data(TRELLO)["api-key"] == f"SECRET-{TRELLO}-api-key"

    def test_a_credential_is_hidden_its_size_shown_and_a_shape_mismatch_asks_first(self):
        store = store_of(**ACTIVATE_NONE)
        store[WIFI].meta["rotation_args"] = '{"what":"PSK","prefix":"psk-"}'
        bao = fake_of(store)
        answers = ("y", "SECRET-no-prefix", "c", "n", "e", "psk-SECRET", "c")
        code, out = run(bao, WIFI, *answers)
        assert code == 0
        assert "16 characters  ⚠ expected: starts with psk-" in out
        assert "The password does not look as expected: it starts with psk-." in out
        assert "10 characters  ✓ starts with psk-" in out
        assert bao.data(WIFI) == {"password": "psk-SECRET"}
        assert "SECRET" not in out

    def test_a_mismatch_continued_anyway_is_taken(self):
        store = store_of(**ACTIVATE_NONE)
        store[WIFI].meta["rotation_args"] = '{"prefix":"psk-"}'
        bao = fake_of(store)
        assert run(bao, WIFI, "y", "SECRET-odd", "c", "y")[0] == 0
        assert bao.data(WIFI) == {"password": "SECRET-odd"}

    def test_an_empty_entry_cannot_continue(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        code, out = run(bao, WIFI, "y", "", "c", "e", "SECRET-psk", "c")
        assert code == 0 and "Every field needs a value: password is empty." in out
        assert bao.data(WIFI) == {"password": "SECRET-psk"}

    def test_a_shown_value_appears_on_reveal_only(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        code, out = run(bao, LEAF, "y", "r", "", "d", kinds=kinds_with(Showing()))
        assert code == 0
        new = bao.data(LEAF)["token"]
        assert out.count(new) == 1
        assert out.index("Store it in RoboForm") < out.index(new)

    def test_exit_at_an_operator_step_leaves_it_in_flight_and_run_takes_it_up(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        code, out = run(bao, WIFI, "y", "SECRET-psk", "x")
        assert code == 0 and "Left in flight" in out
        assert in_flight(bao.meta(WIFI)).step == "operator.credential:password"
        code, out = run(bao, WIFI, "r", "SECRET-psk", "c")
        assert code == 0
        assert "its manual plan of password" in out and "It stopped at step 1 of 3" in out
        assert bao.data(WIFI) == {"password": "SECRET-psk"}

    def test_a_plan_in_flight_holds_back_the_leaf_s_other_plans(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        run(bao, TRELLO, "2", "y", "SECRET-key", "x")
        code, out = run(bao, TRELLO, "x")
        assert "its manual plan of api-key" in out
        assert "Its other plans wait until this one is done or rolled back." in out

    def test_abort_at_an_operator_step_asks_once_then_cancels(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        code, out = run(bao, WIFI, "y", "SECRET-psk", "a", "y")
        assert code == 0 and "Abort? [y/N]" in out and "Cancelled" in out
        assert bao.data(WIFI) == {"password": f"SECRET-{WIFI}-password"}

    def test_end_of_input_at_a_prompt_is_an_exit(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        code, out = run(bao, WIFI, "y")
        assert code == 0 and "Left in flight" in out

    def test_an_all_silent_plan_shows_working(self):
        store = store_of(**ACTIVATE_NONE)
        store[WIFI].meta["rotation_mechanism"] = "random"
        bao = fake_of(store)
        code, out = run(bao, WIFI, "y", kinds=kinds_with(Wrapped(silent_only=True)))
        assert code == 0 and "Working…" in out


class TestFailure:
    def failed(self, *answers, flaky=None):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        kinds = kinds_with(Wrapped(flaky or Flaky(), CHECK))
        return bao, *run(bao, LEAF, "y", *answers, kinds=kinds)

    def test_retry_runs_the_failed_step_again(self):
        bao, code, out = self.failed("r", "d")
        assert code == 0
        assert "✗ do the flaky thing" in out and "    the flaky thing failed" in out
        assert "[r]etry, [a]bort, [d]etails or e[x]it? " in out
        assert "✓ do the flaky thing · flaked" in out

    def test_details_shows_the_technical_detail(self):
        _, code, out = self.failed("d", "x")
        assert code == 1 and "TECHNICAL DETAIL" in out
        assert "It stays stopped at: do the flaky thing." in out

    def test_abort_asks_with_the_rollback_s_count_and_rolls_back(self):
        bao, code, out = self.failed("a", "y")
        assert code == 0
        assert "Abort and roll back 3 steps? [y/N]" in out
        assert "Rolling back" in out and f"✓ undo: write {LEAF}" in out
        assert bao.data(LEAF)["token"] == f"SECRET-{LEAF}-token"
        assert "Rolled back: the rotation of token is undone." in out

    def test_abort_is_not_offered_once_a_finished_step_has_no_undo(self):
        bao, code, out = self.failed("x", flaky=Flaky(undoable=False))
        assert "Abort is not possible: the flaky thing cannot be taken back" in out
        assert "[r]etry, [d]etails or e[x]it? " in out

    def test_a_failure_taken_up_in_a_new_run_offers_the_same(self):
        bao, _, _ = self.failed("x")
        kinds = kinds_with(Wrapped(Flaky(fail=0), CHECK))
        code, out = run(bao, LEAF, "r", "d", kinds=kinds)
        assert code == 0
        assert "It failed at step 4 of 6: do the flaky thing." in out
        assert "    the flaky thing failed" in out

    def test_a_failed_undo_offers_retry_and_details_but_no_abort(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        kinds = kinds_with(Wrapped(Flaky(undo_fail=1)))
        code, out = run(bao, LEAF, "y", "a", "y", "x", kinds=kinds)
        assert code == 1
        assert "✗ undo: do the flaky thing" in out
        assert "Its rollback stays stopped part-way at: do the flaky thing." in out
        assert out.count("[r]etry, [d]etails or e[x]it? ") == 1
        code, out = run(bao, LEAF, "r", kinds=kinds)
        assert code == 0
        assert "Its rollback stopped at step 4 of 5: do the flaky thing." in out
        assert "Rolled back" in out and bao.data(LEAF)["token"] == f"SECRET-{LEAF}-token"


class TestHandActivation:
    """A manual: activation the operator confirmed is part of the rollback, which asks for it
    again once the old value is back."""

    def started(self, *answers):
        store = store_of(
            eso__prd__app__prd__token="manual:restart the app by hand",
            iac__copy="manual:tell the copy's reader",
        )
        bao = fake_of(store)
        return bao, *run(bao, LEAF, "y", "d", "a", "y", *answers)

    def test_abort_after_it_is_confirmed_asks_for_it_again_after_the_undos(self):
        bao, code, out = self.started("d")
        assert code == 0
        assert "Abort and roll back 3 steps? [y/N]" in out
        rollback = out[out.index("Rolling back") :]
        assert rollback.index(f"✓ undo: write {LEAF}") < rollback.index(
            "── again: restart the app by hand"
        )
        assert "[d]one or e[x]it? " in rollback and "[a]bort" not in rollback
        assert "tell the copy's reader" not in rollback
        assert bao.data(LEAF)["token"] == f"SECRET-{LEAF}-token"
        assert "Rolled back: the rotation of token is undone." in out

    def test_exit_there_leaves_the_rollback_for_run_to_continue(self):
        bao, code, out = self.started("x")
        assert code == 0 and "Rolled back:" not in out and "Left in flight" in out
        assert in_flight(bao.meta(LEAF)) is not None
        code, out = run(bao, LEAF, "r", "d")
        assert code == 0
        assert "── again: restart the app by hand" in out
        assert "Rolled back: the rotation of token is undone." in out
        assert in_flight(bao.meta(LEAF)) is None


class Interrupted(Step):
    type = "test.interrupted"

    def __init__(self):
        super().__init__("test.interrupted", "wait for the cluster")

    def run(self, ctx):
        raise KeyboardInterrupt


def test_ctrl_c_in_a_tool_step_leaves_the_plan_in_flight_there():
    store = store_of(**ACTIVATE_NONE)
    bao = fake_of(store)
    code, out = run(bao, LEAF, "y", kinds=kinds_with(Wrapped(Interrupted())))
    assert code == 1
    assert "Interrupted at: wait for the cluster. It is left in flight there" in out
    assert in_flight(bao.meta(LEAF)).step == "test.interrupted"
    assert bao.data(LOCK_LEAF) == {}


class TestTheLock:
    def test_a_dead_holder_s_lock_is_broken_on_yes_and_the_plan_runs(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        Lock(client(bao), "run x on gone, pid 1").take("a plan")
        code, out = run(bao, LEAF, "y", "y")
        assert code == 0
        assert "Another plan runs: run x on gone, pid 1 holds it since" in out
        assert "Is run x on gone, pid 1 gone? Break its lock? [y/N]" in out
        assert "The lock is broken." in out and bao.data(LOCK_LEAF) == {}

    def test_on_no_it_stops_and_the_lock_stays(self):
        store = store_of(**ACTIVATE_NONE)
        bao = fake_of(store)
        Lock(client(bao), "the nightly run").take("a plan")
        code, _ = run(bao, LEAF, "y", "n")
        assert code == 1 and bao.data(LOCK_LEAF)["holder"] == "the nightly run"
        assert bao.data(LEAF)["token"] == f"SECRET-{LEAF}-token"


class TestTheMarker:
    def test_run_confirms_the_rotation_at_its_source(self):
        store = store_of()
        bao = fake_of(store, {SEAL: {"seal-key": MARKER_VALUE}})
        code, out = run(bao, SEAL, "y", "d")
        assert code == 0 and "── Rotate seal-key at its source" in out
        assert bao.data(SEAL)["seal-key"].startswith(MARKER_VALUE + "; rotated ")


class TestHiddenEntry:
    def feed(self, *chunks):
        entry = HiddenEntry()
        results = [entry.feed(chunk) for chunk in chunks]
        return results[-1], results[:-1]

    def test_enter_ends_it_and_backspace_and_ctrl_u_edit_it(self):
        assert self.feed("abX\x7fc\r") == ("abc", [])
        assert self.feed("junk\x15ok\n") == ("ok", [])

    def test_a_bracketed_paste_is_taken_whole_with_its_line_breaks(self):
        paste = f"{PASTE_START}[Interface]\r\nPrivateKey = k\r\nAddress = a\r\n{PASTE_END}"
        assert self.feed(paste, "\r") == ("[Interface]\nPrivateKey = k\nAddress = a", [None])

    def test_a_long_paste_keeps_every_character(self):
        long = "x" * 10000
        assert self.feed(PASTE_START, long[:5000], long[5000:] + PASTE_END + "\r")[0] == long

    def test_an_escape_sequence_split_across_reads_is_put_together(self):
        assert self.feed("\x1b[20", "0~a\rb\x1b[201~", "\r") == ("a\nb", [None, None])

    def test_other_keys_that_send_sequences_do_nothing(self):
        assert self.feed("a\x1b[Db\x1bOc\r")[0] == "abOc"

    def test_ctrl_d_on_an_empty_entry_ends_the_input(self):
        with pytest.raises(EOFError):
            HiddenEntry().feed("\x04")


class TestConsole:
    def test_on_a_tty_a_hidden_entry_is_not_echoed_and_a_long_paste_arrives_whole(self):
        master, slave = pty.openpty()
        con = Console(os.fdopen(slave, "r"), os.fdopen(os.dup(slave), "w"))
        assert con.tty
        value = "x" * 6000 + "\nlast line"

        def paste():
            for _ in range(1000):
                if not termios.tcgetattr(slave)[3] & termios.ECHO:
                    break
                select.select([], [], [], 0.01)
            os.write(master, f"{PASTE_START}{value}{PASTE_END}\r".encode())

        entered = []
        reader = threading.Thread(target=lambda: entered.append(con.hidden("token: ")))
        writer = threading.Thread(target=paste, daemon=True)
        reader.start(), writer.start()
        reader.join(10)
        if reader.is_alive():  # a read that waits for more: closing the terminal ends it
            os.close(master)
            reader.join()
            pytest.fail("the hidden entry never ended")
        assert entered == [value]
        shown = b""
        while select.select([master], [], [], 0)[0]:
            shown += os.read(master, 65536)
        assert b"token: " in shown and b"xxxx" not in shown and b"last line" not in shown
        assert termios.tcgetattr(slave)[3] & termios.ECHO
        con.stdin.close(), con.stdout.close(), os.close(master)

    def test_without_a_tty_a_hidden_entry_is_a_line_read_and_never_written(self):
        con = console("SECRET-typed")
        assert con.hidden("token: ") == "SECRET-typed"
        assert output(con) == "token: "

    def test_the_end_of_input_is_eof(self):
        with pytest.raises(EOFError):
            console().ask("? ")
        with pytest.raises(EOFError):
            console().hidden("? ")


def test_the_tool_part_names_copies_and_activation():
    store = store_of(**ACTIVATE_NONE)
    plan = make(KINDS, LEAF, "random", ["token"], store, audit(store))
    assert tool_part(plan.target) == "writes it to the leaf and its 1 copy"
