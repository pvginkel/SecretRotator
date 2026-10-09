"""The step-ca-password kind (design §6): a new password for step-ca's kubecoder-jwk provisioner,
under which the operator gives kubecoder-jwk a new key pair, since a key only re-encrypted still
signs for whoever holds the old password (050 I2). The tool reads the key step-ca serves,
generates the password and shows it; the operator replaces the key pair in the step_ca role's
ca.json by the runbook and runs the step-ca playbook. Done runs the CA check (052 F1), and only
once it passes does the tool write the password to the leaf and its copies and activate: the
KubeCoder controllers read it at start."""

from collections.abc import Callable, Mapping

from secret_rotator.kinds.step_ca_password.stepca import BASE, STEP, StepCa
from secret_rotator.kinds.step_ca_password.steps import CheckedShow, ReadKey
from secret_rotator.model import Step, value_name
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

PROVISIONER = "kubecoder-jwk"
TITLE = f"Give {PROVISIONER} a new key pair under the new password"
INSTRUCTION = (
    f"The new password of step-ca's {PROVISIONER} provisioner. Follow Ansible "
    f'docs/runbooks/step-ca-bootstrap.md, "JWK provisioner password rotation", for {PROVISIONER}: '
    f"give it a new key pair under this password in the step_ca role's ca.json, run "
    f"playbooks/step-ca.yml and commit. Done then checks that step-ca serves the new key and that "
    f"this password opens it."
)
IRREVERSIBLE = f"step-ca serves {PROVISIONER}'s new key pair, which only the new password opens"


class StepCaPassword:
    name = "step-ca-password"
    per_key = False

    def __init__(self, opener: Callable | None = None, step: tuple[str, ...] = STEP):
        self.opener = opener  # step-ca's HTTP opener; None: the real one
        self.step = step  # the step-cli command

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: step-ca-password takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(
            [f"give {PROVISIONER} a new key pair under the new password", *leaf.confirms]
        )

    def credential(self, leaf: Target) -> str:
        return "step-ca provisioner password"

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool generates a new password for step-ca's {PROVISIONER} provisioner. You give "
            f"{PROVISIONER} a new key pair under it and run the step-ca playbook; Done checks "
            f"that step-ca serves it. The tool then {tool_part(leaf)}.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a step-ca-password plan rotates one key, not {len(leaf.keys)}"
            )
        key, ca = leaf.keys[0], StepCa(BASE, self.opener, self.step)
        return [
            ReadKey(ca, PROVISIONER),
            *ctx.steps.generate(key),
            CheckedShow(
                ca, PROVISIONER, value_name(key), TITLE, INSTRUCTION, irreversible=IRREVERSIBLE
            ),
            *ctx.steps.write(),
            *ctx.steps.activate(),
        ]
