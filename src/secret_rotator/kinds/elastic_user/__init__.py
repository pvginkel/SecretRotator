"""The elastic-user kind (design §6): a new password for an Elasticsearch user, the superuser
elastic among them, whose leaf is also the kind's counterpart (044 D3). Elasticsearch takes a
user's password from its API alone; its setup Job sets each user's from the user's Secret when it
runs. Its plan logs in with the passwords the leaf and the counterpart hold, which stops it before
it writes when Elasticsearch refuses one. It generates the new password, writes it to the leaf and
its copies, and syncs every ExternalSecret that reads them, whatever the leaf's activate. Only then
does it set the password in Elasticsearch, so no Secret holds a password Elasticsearch no longer
takes (R102), and it activates last: every consumer reads its password when it starts."""

import re
from collections.abc import Callable, Mapping

from secret_rotator.cluster import Workload
from secret_rotator.kinds.elastic_user.elasticsearch import Elasticsearch
from secret_rotator.kinds.elastic_user.steps import COUNTERPART, SUPERUSER, Login, SetPassword
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

# The address Elasticsearch is served at off the cluster: ElasticsearchDeploy README.md's.
BASE = "http://elasticsearch.home"
ARGS = ("user",)
USER = re.compile(r"[A-Za-z0-9_.@-]+")
# Elasticsearch's startupProbe alone gives it five minutes to start, k8s.rollout's whole default
# bound (ElasticsearchDeploy chart/templates/elasticsearch-deployment.yaml).
ELASTICSEARCH = Workload("elasticsearch-prd", "deployment", "elasticsearch")
BOUNDS = {ELASTICSEARCH: 600}


def user_of(leaf: Target) -> str:
    """The user of the plan's one key, password."""
    return leaf.entries[leaf.keys[0]].args["user"]


class ElasticUser:
    name = "elastic-user"
    per_key = False

    def __init__(self, opener: Callable | None = None):
        self.opener = opener  # Elasticsearch's HTTP opener; None: the real one

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [
            f"{k}: not one of elastic-user's {', '.join(ARGS)}" for k in args if k not in ARGS
        ]
        user = args.get("user")
        if user is None:
            problems.append("user: missing; the Elasticsearch user whose password it is")
        elif not (isinstance(user, str) and USER.fullmatch(user)):
            problems.append("user: not an Elasticsearch user name")
        return problems

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "Elasticsearch user password"

    def description(self, leaf: Target) -> str:
        user = user_of(leaf)
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        login = (
            "the one it held" if leaf.leaf == COUNTERPART else f"the password {COUNTERPART} holds"
        )
        before = " and before it activates" if leaf.activates else ""
        return (
            f"The tool generates a new password for Elasticsearch user {user} and "
            f"{tool_part(leaf)}. Once every ExternalSecret that reads the leaf has synced{before}, "
            f"it sets the password in Elasticsearch as user {SUPERUSER}, logged in with {login}, "
            f"and logs in with the new one.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        user, key = user_of(leaf), leaf.keys[0]
        if (leaf.leaf == COUNTERPART) != (user == SUPERUSER):
            raise PlanError(
                f"{leaf.leaf}: user {SUPERUSER}'s password is {COUNTERPART}'s, the kind's "
                f"counterpart, and no other leaf's"
            )
        es = Elasticsearch(BASE, self.opener)
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            Login(es, leaf.leaf, key, user),
            *ctx.steps.generate(key),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
            SetPassword(es, leaf.leaf, key, user),
        ]
        planned = {step.id for step in steps}
        activation = ctx.steps.activate(bounds=BOUNDS)
        return [*steps, *(step for step in activation if step.id not in planned)]
