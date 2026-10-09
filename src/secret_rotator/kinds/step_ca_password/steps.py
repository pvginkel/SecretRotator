"""The step-ca-password kind's own steps (design §4.2: custom steps are the plugin's):
step_ca.read_key, which keeps the key step-ca served the provisioner under when the plan started,
and the operator.show of the new password whose Done runs the CA check against that key. No detail
or error they report carries a password or a private key."""

from secret_rotator.kinds.step_ca_password.stepca import NotOpened, StepCa, StepCaError, material
from secret_rotator.model import Context, Step, StepFailed
from secret_rotator.opsteps import OperatorShow, ShowRequest

# The staging name of the key step-ca served the provisioner under when the plan started: its
# material, public.
AT_START = "step-ca-password:key-at-start"
# The instruction's last paragraph while the check fails.
FAILED = "The CA check failed: {}. Redo the key pair or the playbook, then press Done again."


class ReadKey(Step):
    """Reads the key step-ca serves the provisioner under and stages it as the one served when
    the plan started, which the CA check compares against. A re-run with one staged keeps it, so
    neither a Retry nor a Resume moves the start."""

    type = "step_ca.read_key"
    silent = True

    def __init__(self, ca: StepCa, provisioner: str):
        super().__init__(
            f"step_ca.read_key:{provisioner}", f"read the {provisioner} key step-ca serves"
        )
        self.ca = ca
        self.provisioner = provisioner

    def run(self, ctx: Context) -> str:
        if ctx.staged(AT_START) is not None:
            return "read before"
        try:
            key, _ = self.ca.jwk(self.provisioner)
            served = material(key)
        except StepCaError as e:
            raise StepFailed(str(e)) from None
        ctx.stage(AT_START, served)
        return f"step-ca serves {self.provisioner} under key {key.get('kid', '(no kid)')}"


class CheckedShow(OperatorShow):
    """The operator.show of the new password, under which the operator gives the provisioner a new
    key pair and puts it in step-ca. Done runs the CA check: step-ca serves the provisioner under a
    key other than the one ReadKey staged, and the new password opens that key. While the check
    fails the step asks again, the check's reason under its instruction, so the password stays
    revealable and Abort open; only a Done whose check passes finishes it. It mutates and has no
    undo: once the plan is past it, Abort is disabled (design §4.5)."""

    def __init__(
        self,
        ca: StepCa,
        provisioner: str,
        name: str,
        title: str,
        instruction: str,
        *,
        irreversible: str,
    ):
        super().__init__(name, title, instruction, irreversible=irreversible)
        self.ca = ca
        self.provisioner = provisioner

    def run(self, ctx: Context) -> str:
        value = ctx.staged(self.name)
        if value is None:
            raise StepFailed(f"no value is staged as {self.name}")
        start = ctx.staged(AT_START)
        if start is None:
            raise StepFailed(f"no {self.provisioner} key of the plan's start is staged")
        instruction = self.instruction
        while True:
            ctx.ask(ShowRequest(self.title, instruction, value))
            ctx.progress(f"checking the {self.provisioner} key step-ca serves")
            why = self.why_not(start, value)
            # Stops the step here when an Abort or an exit was asked for while the check ran.
            ctx.progress(why or "the check passed")
            if why is None:
                return f"step-ca serves a new {self.provisioner} key, which the new password opens"
            instruction = f"{self.instruction}\n\n{FAILED.format(why)}"

    def why_not(self, start: str, password: str) -> str | None:
        """Why the CA check fails; None when it passes."""
        name = self.provisioner
        try:
            key, encrypted = self.ca.jwk(name)
            served = material(key)
            if served == start:
                return (
                    f"step-ca serves {name} under the key it served when the rotation started: "
                    f"the playbook has not run, or the key was re-encrypted, not replaced"
                )
            if self.ca.opens(encrypted, password) != served:
                return f"the key the new password opens is not the {name} key step-ca serves"
        except NotOpened as e:
            return f"the new password does not open the {name} key step-ca serves: {e}"
        except StepCaError as e:
            return str(e)
        return None
