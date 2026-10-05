"""The three operator steps of design §4.2, the only operator interaction there is. Each asks the
renderer through ctx.ask with its request; the renderer answers, or the operator abandons the step
by Abort or exit, which the executor takes over. They do not fail on the operator's account."""

from collections.abc import Callable
from dataclasses import dataclass, field

from secret_rotator.model import Actor, Context, Step, StepFailed, value_name


@dataclass(frozen=True)
class Shape:
    """What an entered value is expected to look like; a value that does not match it asks before
    it is taken, and is never refused (design R53)."""

    words: str  # `starts with github_pat_`
    test: Callable[[str], bool]


def starts_with(prefix: str) -> Shape:
    return Shape(f"starts with {prefix}", lambda value: value.startswith(prefix))


@dataclass(frozen=True)
class Field:
    key: str
    shape: Shape | None = None


@dataclass(frozen=True)
class CredentialRequest:
    """Enter one value per field, masked; answered with each field's value, none empty."""

    title: str
    instruction: str  # where to mint it, with which scopes
    fields: tuple[Field, ...]


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
    value, for the kv.write after it. A value staged before an exit or a crash is kept."""

    type = "operator.credential"
    actor = Actor.OPERATOR

    def __init__(
        self, keys: tuple[str, ...], title: str, instruction: str, shape: Shape | None = None
    ):
        super().__init__(f"operator.credential:{','.join(keys)}", title)
        self.keys = keys
        self.instruction = instruction
        self.shape = shape

    def run(self, ctx: Context) -> str:
        names = {key: value_name(key) for key in self.keys}
        if all(ctx.staged(name) is not None for name in names.values()):
            return "entered before"
        fields = tuple(Field(key, self.shape) for key in self.keys)
        answer = ctx.ask(CredentialRequest(self.title, self.instruction, fields))
        if empty := [key for key in self.keys if not answer.get(key)]:
            raise StepFailed(f"no value was entered for {', '.join(empty)}")
        for key, name in names.items():
            ctx.stage(name, answer[key])
        return ", ".join(f"{key}: {len(answer[key])} characters" for key in self.keys)


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
