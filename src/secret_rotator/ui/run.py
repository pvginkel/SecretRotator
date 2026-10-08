"""One plan run from the UI: the executor and its session on a thread of their own, since the
executor holds the lock while it waits on the operator (design §4.3). Its events, asks, lines and
questions reach the app as messages; the app answers an ask or a question through answer(), which
the thread waits on."""

import queue
import threading
from collections.abc import Callable

from textual.app import App
from textual.message import Message

from secret_rotator.executor import Abandon, Executor, Outcome, Renderer
from secret_rotator.model import Event, Step
from secret_rotator.plan import Plan
from secret_rotator.session import Session
from secret_rotator.ui.item import Item

# The command a plan left in flight is taken up in.
COMMAND = "secret-rotator ui"

ExecutorOf = Callable[[Plan, Renderer], Executor]


class Run:
    def __init__(self, app: App, item: Item, executor_of: ExecutorOf):
        self.app = app
        self.item = item
        self.executor = executor_of(item.rotation.plan, self)
        self.session = Session(
            self.executor, say=self.say, confirm=self.confirm, notify=None, command=COMMAND
        )
        # The app's answer to the one ask or question outstanding.
        self.answers: queue.Queue = queue.Queue()
        self.started = False  # a step started: the plan is in flight
        self.thread = threading.Thread(target=self._main, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def answer(self, answer: dict[str, str] | Abandon | bool) -> None:
        self.answers.put(answer)

    def _main(self) -> None:
        self.app.post_message(Run.Ended(self, self.session.attempt(self.executor.run)))

    # --- the executor's renderer and the session's front end, on the run's thread -------------

    def event(self, event: Event) -> None:
        self.started = True
        self.app.post_message(Run.Stepped(self, event))

    def ask(self, step: Step, request: object) -> dict[str, str] | Abandon:
        self.app.post_message(Run.Asked(self, step, request))
        return self.answers.get()

    def say(self, line: str) -> None:
        self.app.post_message(Run.Said(self, line))

    def confirm(self, question: str) -> bool:
        self.app.post_message(Run.Questioned(self, question))
        return self.answers.get()

    # --- the messages -----------------------------------------------------------------------

    class Stepped(Message):
        def __init__(self, run: "Run", event: Event):
            super().__init__()
            self.run = run
            self.event = event

    class Asked(Message):
        def __init__(self, run: "Run", step: Step, request: object):
            super().__init__()
            self.run = run
            self.step = step
            self.request = request

    class Said(Message):
        def __init__(self, run: "Run", line: str):
            super().__init__()
            self.run = run
            self.line = line

    class Questioned(Message):
        def __init__(self, run: "Run", question: str):
            super().__init__()
            self.run = run
            self.question = question

    class Ended(Message):
        """outcome: None when the session got to none, which it said."""

        def __init__(self, run: "Run", outcome: Outcome | None):
            super().__init__()
            self.run = run
            self.outcome = outcome
