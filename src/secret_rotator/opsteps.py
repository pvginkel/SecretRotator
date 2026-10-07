"""The three operator steps of design §4.2, the only operator interaction there is. Each asks the
renderer through ctx.ask with its request; the renderer answers, or the operator abandons the step
by Abort or exit, which the executor takes over. They do not fail on the operator's account."""

import datetime
from collections.abc import Callable
from dataclasses import dataclass, field

from secret_rotator.contract import ContractError, parse_date
from secret_rotator.model import Actor, Context, Step, StepFailed, expiry_name, value_name

# The answer's name of the new credential's expiry, beside the fields' keys.
EXPIRY = "expires_at"


@dataclass(frozen=True)
class Shape:
    """What an entered value is expected to look like; a value that does not match it asks before
    it is taken, and is never refused (design R53)."""

    words: str  # after `it`: `starts with ghp_ or github_pat_`
    test: Callable[[str], bool]


@dataclass(frozen=True)
class Field:
    key: str
    shape: Shape | None = None


@dataclass(frozen=True)
class CredentialRequest:
    """Enter one value per field, masked; answered with each field's value, none empty. One that
    expires also asks the new credential's expiry, answered under EXPIRY: an ISO date after today,
    or empty for none."""

    title: str
    instruction: str  # where to mint it, with which scopes
    fields: tuple[Field, ...]
    expires: bool = False


@dataclass(frozen=True)
class ShowRequest:
    """Put the value where only a human can; shown on request only (reveal), answered when done."""

    title: str
    instruction: str  # where it goes
    value: str = field(repr=False)


@dataclass(frozen=True)
class ConfirmRequest:
    """Do something and confirm it is done."""

    title: str
    instruction: str


class OperatorCredential(Step):
    """The operator mints a value elsewhere and enters it: each key's value is staged as its new
    value, for the kv.write after it. A value staged before an exit or a crash is kept. For a
    credential that expires (design §6) it asks the expiry too, staged for each key, empty for
    none, for kv.stamp to write as its expires_at; staged before the values, so a value staged
    has its expiry staged."""

    type = "operator.credential"
    actor = Actor.OPERATOR

    def __init__(
        self,
        keys: tuple[str, ...],
        title: str,
        instruction: str,
        shape: Shape | None = None,
        *,
        expires: bool = False,
    ):
        super().__init__(f"operator.credential:{','.join(keys)}", title)
        self.keys = keys
        self.instruction = instruction
        self.shape = shape
        self.expires = expires

    def run(self, ctx: Context) -> str:
        names = {key: value_name(key) for key in self.keys}
        if all(ctx.staged(name) is not None for name in names.values()):
            return "entered before"
        fields = tuple(Field(key, self.shape) for key in self.keys)
        answer = ctx.ask(CredentialRequest(self.title, self.instruction, fields, self.expires))
        if empty := [key for key in self.keys if not answer.get(key)]:
            raise StepFailed(f"no value was entered for {', '.join(empty)}")
        entered = ", ".join(f"{key}: {len(answer[key])} characters" for key in self.keys)
        if self.expires:
            expiry = answer[EXPIRY]
            if expiry and (problem := expiry_problem(expiry, ctx.now.date())):
                raise StepFailed(f"the expiry entered is no expiry: {problem}")
            for key in self.keys:
                ctx.stage(expiry_name(key), expiry)
            entered += f"; expires {expiry}" if expiry else "; no expiry"
        for key, name in names.items():
            ctx.stage(name, answer[key])
        return entered


def expiry_problem(text: str, today: datetime.date) -> str | None:
    """What keeps the text from being a new credential's expiry, an ISO date after today; None
    when it is one."""
    try:
        date = parse_date(text)
    except ContractError as e:
        return str(e)
    return None if date > today else f"{date} is not after today, {today}"


class OperatorShow(Step):
    """The operator takes a value the tool produced, staged as `name`, and puts it where only a
    human can (RoboForm, srviac's secrets.yaml). One whose value cannot be taken back once put in
    place (a secret_id whose predecessor the tool does not know) is irreversible: it mutates and
    has no undo, so it disables Abort once done."""

    type = "operator.show"
    actor = Actor.OPERATOR

    def __init__(self, name: str, title: str, instruction: str, *, irreversible: str = ""):
        super().__init__(f"operator.show:{name}", title)
        self.name = name
        self.instruction = instruction
        if irreversible:
            self.mutates = True
            self.no_undo = irreversible

    def run(self, ctx: Context) -> str:
        value = ctx.staged(self.name)
        if value is None:
            raise StepFailed(f"no value is staged as {self.name}")
        ctx.ask(ShowRequest(self.title, self.instruction, value))
        return "done"


class OperatorConfirm(Step):
    """The operator does something and confirms it. One whose action cannot be taken back (a token
    revoked at its vendor) is irreversible: it mutates and has no undo, so it disables Abort once
    done (design §4.5). A hand activation (a manual: activator) is an activator: it mutates, and a
    rollback asks for it again after the undos."""

    type = "operator.confirm"
    actor = Actor.OPERATOR

    def __init__(
        self,
        id: str,
        title: str,
        instruction: str = "",
        *,
        irreversible: str = "",
        activator: bool = False,
    ):
        super().__init__(f"operator.confirm:{id}", title)
        self.instruction = instruction
        if irreversible:
            self.mutates = True
            self.no_undo = irreversible
        if activator:
            self.mutates = self.activator = True

    def run(self, ctx: Context) -> str:
        ctx.ask(ConfirmRequest(self.title, self.instruction))
        return "done"
