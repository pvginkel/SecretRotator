"""The approle kind (design §6): a new secret_id for one of the seven AppRoles (catalog
§ rotator/). Its plan mints it with an expiry of four times the key's interval and never less than
90 days, proves it by a login, delivers it as rotation_args says, rewrites the leaf, activates, and
last destroys the secret_id the consumer held before. With the kv delivery the leaf holds the
secret_id itself (iac/rotator-approle); with any other it is a marker leaf (rotator/approle/*),
whose key the plan rewrites with the marker text."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from secret_rotator.ansiblesteps import Playbook
from secret_rotator.kinds.approle.steps import (
    SECRET,
    DestroyOldAccessor,
    Held,
    K8sSecret,
    Login,
    Mint,
    held_in_kv,
)
from secret_rotator.model import Step, value_name
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part
from secret_rotator.schedule import interval_of

ARGS = ("role", "delivery")
ROLE = re.compile(r"[A-Za-z0-9._-]+")
DELIVERY = re.compile(
    r"(?P<plain>kv|playbook)|(?P<how>k8s_secret|jenkins_credential|manual)=(?P<arg>.*\S.*)"
)
NAME = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
SECRET_REF = re.compile(rf"{NAME}/[a-z0-9]([-.a-z0-9]*[a-z0-9])?")
CREDENTIAL_ID = re.compile(r"\S+")
DELIVERIES = (
    "kv, k8s_secret=<namespace>/<name>, jenkins_credential=<id>, playbook or manual=<where>"
)
# A secret_id expires after four times its key's interval, and never less than 90 days: a safety
# net against a forgotten secret_id, not what drives its rotation.
EXPIRY_FACTOR, MIN_EXPIRY_DAYS = 4, 90
# The playbook delivery is the openbao role's scoped backup delivery (Ansible
# playbooks/openbao-backup-secret-id.yml): the backup role's secret_id to the srvvaults.
BACKUP = "backup"
BACKUP_PLAYBOOK = "playbooks/openbao-backup-secret-id.yml"
BACKUP_TAG = "openbao_backup_secret_id"
BACKUP_VAR = "openbao_backup_secret_id"


@dataclass(frozen=True)
class Delivery:
    how: str  # kv, k8s_secret, jenkins_credential, playbook or manual
    arg: str = ""


def delivery_of(text: object) -> Delivery | None:
    """rotation_args' delivery; None when it is none of DELIVERIES."""
    found = DELIVERY.fullmatch(text) if isinstance(text, str) else None
    if found is None:
        return None
    if found["plain"]:
        return Delivery(found["plain"])
    delivery = Delivery(found["how"], found["arg"])
    if delivery.how == "k8s_secret" and not SECRET_REF.fullmatch(delivery.arg):
        return None
    if delivery.how == "jenkins_credential" and not CREDENTIAL_ID.fullmatch(delivery.arg):
        return None
    return delivery


def expiry_days(leaf: Target) -> int | None:
    """The ttl the plan mints with, in days; None for a key that rotates never."""
    interval = interval_of(leaf.meta, leaf.keys[0])
    return None if interval is None else max(EXPIRY_FACTOR * interval, MIN_EXPIRY_DAYS)


def consumer(leaf: Target, delivery: Delivery) -> str:
    """Who holds the secret_id, in words."""
    return {
        "kv": leaf.leaf,
        "k8s_secret": f"Secret {delivery.arg}",
        "jenkins_credential": f"Jenkins credential {delivery.arg}",
        "playbook": "the srvvaults",
        "manual": "its consumer",
    }[delivery.how]


class AppRole:
    name = "approle"
    per_key = False

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [f"{k}: not one of approle's {', '.join(ARGS)}" for k in args if k not in ARGS]
        role = args.get("role")
        if not isinstance(role, str) or not ROLE.fullmatch(role):
            problems.append("role: not an AppRole name")
        delivery = delivery_of(args.get("delivery"))
        if delivery is None:
            problems.append(f"delivery: not {DELIVERIES}")
        elif delivery.how == "playbook" and role != BACKUP:
            problems.append(f"delivery: playbook delivers the {BACKUP} role's secret_id only")
        return problems

    def ask(self, leaf: Target) -> str:
        delivery = delivery_of(leaf.args["delivery"])
        put = [f"put the new {leaf.args['role']} secret_id in place"]
        return "; ".join([*(put if delivery.how == "manual" else []), *leaf.confirms])

    def description(self, leaf: Target) -> str:
        role, delivery = leaf.args["role"], delivery_of(leaf.args["delivery"])
        minted = f"The tool mints a new {role} secret_id that expires in {expiry_days(leaf)} days"
        activates = " and activates what reads it" if leaf.activates else ""
        then = f"then destroys the one {consumer(leaf, delivery)} held before"
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        if delivery.how == "manual":
            return (
                f"{minted} and logs in with it. You put it in place: {delivery.arg}. The tool "
                f"records the rotation on this marker leaf{activates}, {then}.{confirm}"
            )
        delivers = {
            "kv": tool_part(leaf),
            "k8s_secret": f"writes it into Secret {delivery.arg}{activates}",
            "jenkins_credential": f"writes it into Jenkins credential {delivery.arg}{activates}",
            "playbook": f"delivers it to the srvvaults by the openbao role's scoped backup "
            f"delivery{activates}",
        }[delivery.how]
        return f"{minted}, logs in with it, {delivers}, {then}.{confirm}"

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(f"{leaf.leaf}: an approle plan rotates one key, not {len(leaf.keys)}")
        role, delivery = leaf.args["role"], delivery_of(leaf.args["delivery"])
        days = expiry_days(leaf)
        if days is None:
            raise PlanError(
                f"{leaf.leaf}: {leaf.keys[0]} rotates never, and an AppRole secret_id is minted "
                f"with an expiry of {EXPIRY_FACTOR} times its interval"
            )
        own = delivery.how == "kv"
        secret = value_name(leaf.keys[0]) if own else SECRET
        held, deliver = self._delivery(leaf, ctx, delivery, secret)
        who = consumer(leaf, delivery)
        return [
            *([] if own else ctx.steps.marker()),
            Mint(role, leaf.leaf, secret, days, who, held),
            Login(role, secret),
            *deliver,
            *ctx.steps.write(),
            *ctx.steps.activate(),
            DestroyOldAccessor(role, who),
        ]

    def _delivery(
        self, leaf: Target, ctx: PlanContext, delivery: Delivery, secret: str
    ) -> tuple[Held | None, list[Step]]:
        """Where the consumer's secret_id can be read (None: it cannot), and the steps that hand it
        the new one: none for kv, whose kv.write is its delivery."""
        role = leaf.args["role"]
        if delivery.how == "kv":
            return held_in_kv(leaf.leaf, leaf.keys[0]), []
        if delivery.how == "k8s_secret":
            if ctx.steps.cluster is None:
                raise PlanError(
                    f"{leaf.leaf}: its delivery writes Secret {delivery.arg}, on the cluster an "
                    f"offline plan does not reach"
                )
            namespace, name = delivery.arg.split("/")
            step = K8sSecret(ctx.steps.cluster, namespace, name, secret)
            return step.held, [step]
        if delivery.how == "jenkins_credential":
            return None, ctx.steps.jenkins_credential(delivery.arg, secret)
        if delivery.how == "playbook":
            book = Playbook(BACKUP_PLAYBOOK, tags=(BACKUP_TAG,), staged={BACKUP_VAR: secret})
            return None, ctx.steps.playbook(
                "openbao-backup-secret-id",
                "deliver the backup secret_id to the srvvaults",
                book,
                no_undo="the srvvaults' previous backup secret_id is not known to the rotator: "
                "it cannot be written back",
            )
        return None, ctx.steps.show(
            secret,
            f"Put the new {role} secret_id in place",
            delivery.arg,
            irreversible=f"the new {role} secret_id is in place, and the rotator does not know "
            f"the one it replaced",
        )
