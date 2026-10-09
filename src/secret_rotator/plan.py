"""Building a plan (design §4.3): the entries of the keys it writes resolved into a Target, the
kind's steps built with the step factory, and kv.stamp appended by the core. A leaf has one plan
per kind with a plugin, one per key for a per-key kind (manual, external)."""

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from secret_rotator.ansiblesteps import Ansible, AnsibleRun, Playbook
from secret_rotator.audit import Audit, Leaf
from secret_rotator.cluster import Cluster, Derived, Ref, Workload
from secret_rotator.contract import NONE, Activator, Entry, copy_target, entry_name, is_scheduled
from secret_rotator.jenkins import Jenkins
from secret_rotator.jenkinssteps import JenkinsCredential, JenkinsJob, parse_job
from secret_rotator.k8ssteps import EsoSync, K8sRollout
from secret_rotator.kvsteps import (
    DEFAULT_LENGTH,
    URLSAFE,
    KvCopy,
    KvStamp,
    KvWrite,
    Marker,
    RandomGenerate,
)
from secret_rotator.model import Actor, Step
from secret_rotator.opsteps import OperatorConfirm, OperatorCredential, OperatorShow, Shape
from secret_rotator.schedule import schedule
from secret_rotator.sshsteps import Ssh, SshSetPassword
from secret_rotator.staging import InFlight


class PlanError(Exception):
    pass


@dataclass(frozen=True, order=True)
class Copy:
    leaf: str
    key: str
    of: str  # the primary key it copies


@dataclass(frozen=True)
class Activation:
    """The activate of an entry the plan writes: a rotated key's, or a copy's on its leaf."""

    leaf: str
    key: str
    specs: tuple[Activator, ...]


@dataclass(frozen=True)
class Target:
    """What a kind plans from: one kind's keys on one leaf, with their entries."""

    leaf: str
    kind: str
    keys: tuple[str, ...]  # the keys the plan rotates, sorted
    entries: Mapping[str, Entry]  # each of those keys' entry
    copies: tuple[Copy, ...]  # every copy of those keys, sorted
    # Every entry the plan writes: the rotated keys' first, then the copies', leaf by leaf.
    activations: tuple[Activation, ...]

    @property
    def activates(self) -> bool:
        """Whether an entry the plan writes names any activation."""
        return any(a.specs for a in self.activations)

    @property
    def confirms(self) -> tuple[str, ...]:
        """The texts of the manual: activators, each an operator.confirm of the plan: one per text
        and leaf."""
        found = ((a.leaf, s.arg) for a in self.activations for s in a.specs if s.name == "manual")
        return tuple(text for _, text in dict.fromkeys(found))


def tool_part(leaf: Target) -> str:
    """What the tool does with the new value, for a kind's description: `writes it to the leaf and
    its 2 copies and activates what reads it`."""
    n = len(leaf.copies)
    copies = "" if n == 0 else f" and its {n} cop{'y' if n == 1 else 'ies'}"
    activates = " and activates what reads it" if leaf.activates else ""
    return f"writes it to the leaf{copies}{activates}"


KUBERNETES = ("eso", "k8s-rollout")


class StepFactory:
    """The patterns a plan is built from, named by what they do (design §4.3). Without a cluster,
    as offline without a snapshot, it builds no Kubernetes step. What derived holds of a leaf is
    not read from the cluster again: a plan in flight is rebuilt from what it derived when it
    started (design §4.5). Jenkins, Ansible and SSH are reached only when a step runs: by default
    the real ones."""

    def __init__(
        self,
        target: Target,
        cluster: Cluster | None = None,
        *,
        derived: Derived | None = None,
        jenkins: Jenkins | None = None,
        ansible: Ansible | None = None,
        ssh: Ssh | None = None,
    ):
        self.target = target
        self.cluster = cluster
        # What the plan derived from the cluster, each leaf read once: its record.
        self.derived = Derived()
        if derived is not None:
            self.derived = Derived(dict(derived.externalsecrets), dict(derived.workloads))
        self.jenkins = jenkins or Jenkins()
        self.ansible = ansible or Ansible()
        self.ssh = ssh or Ssh()
        # What the activation read from the cluster, for the leaf's consumers in the run state: the
        # ExternalSecrets it syncs and the workloads it derived; a named target is in an activate
        # already.
        self.consumers: list[str] = []

    def generate(
        self, key: str, *, length: int = DEFAULT_LENGTH, charset: str = URLSAFE
    ) -> list[Step]:
        """A new random value for one of the plan's keys."""
        return [RandomGenerate(key, length=length, charset=charset)]

    def write(self) -> list[Step]:
        """kv.write of the plan's keys to the leaf, then a kv.copy per copy key."""
        t = self.target
        return [KvWrite(t.leaf, t.keys), *(KvCopy(c.leaf, c.key, c.of) for c in t.copies)]

    def marker(self) -> list[Step]:
        """On a marker leaf, the new marker text of each of the plan's keys, for write()."""
        t = self.target
        return [Marker(t.kind, t.leaf, key) for key in t.keys]

    def credential(
        self, title: str, instruction: str, shape: Shape | None = None, *, expires: bool = False
    ) -> list[Step]:
        """The operator mints the plan's keys elsewhere and enters them, one masked input each;
        expires: with the new credential's expiry."""
        t = self.target
        return [OperatorCredential(t.leaf, t.keys, title, instruction, shape, expires=expires)]

    def show(
        self, name: str, title: str, instruction: str, *, irreversible: str = ""
    ) -> list[Step]:
        """The operator puts the value staged as `name` where only a human can; irreversible: why
        it cannot be taken back once there."""
        return [OperatorShow(name, title, instruction, irreversible=irreversible)]

    def confirm(
        self, id: str, title: str, instruction: str = "", *, irreversible: str = ""
    ) -> list[Step]:
        """The operator does something and confirms it; irreversible: why it cannot be undone."""
        return [OperatorConfirm(id, title, instruction, irreversible=irreversible)]

    def jenkins_job(self, job: str, params: Mapping[str, str]) -> list[Step]:
        """A build of the job by its full name, with its parameters, that must succeed."""
        return [JenkinsJob(self.jenkins, job, params)]

    def jenkins_credential(self, credential: str, staged: str) -> list[Step]:
        """The value staged under that name into a Jenkins credential, by its id; no undo."""
        return [JenkinsCredential(self.jenkins, credential, staged=staged)]

    def playbook(
        self,
        name: str,
        title: str,
        book: Playbook,
        counter: Playbook | None = None,
        *,
        no_undo: str = "",
    ) -> list[Step]:
        """A playbook run, its id ansible.run:<name>; counter: the run that undoes it."""
        return [AnsibleRun(self.ansible, name, title, book, counter, no_undo=no_undo)]

    def set_password(self, key: str, user: str, hosts: Iterable[str]) -> list[Step]:
        """An ssh.set_password of the user to the new value of one of the plan's keys, one per
        host; each undo sets back the one the leaf holds, so the plan puts them before write()."""
        return [SshSetPassword(self.ssh, host, user, self.target.leaf, key) for host in hosts]

    def eso_sync_and_rollout(
        self, targets: Iterable[Workload], leaves: Iterable[str] | None = None
    ) -> list[Step]:
        """An eso.sync of every ExternalSecret that references the leaves (the plan's leaf by
        default), then a k8s.rollout of each target: every sync before every rollout."""
        cluster = self._cluster()
        leaves = (self.target.leaf,) if leaves is None else tuple(leaves)
        found = [es for leaf in leaves for es in self._external_secrets(leaf)]
        syncs = [EsoSync(cluster, es) for es in dict.fromkeys(found)]
        targets = list(dict.fromkeys(targets))
        if cluster.snapshot:
            held = {workload for workload, _ in cluster.workloads}
            if absent := [str(w) for w in targets if w not in held]:
                raise PlanError(
                    f"{self.target.leaf}: the snapshot holds no rollout target {', '.join(absent)}"
                )
        return [*syncs, *(K8sRollout(cluster, w) for w in targets)]

    def activate(self) -> list[Step]:
        """The activation of every entry the plan writes, in the Target's order: each spec of its
        activate as its steps. The Kubernetes specs of all of them are one eso_sync_and_rollout,
        at the first one's place, since a workload may read the Secrets of several of their leaves;
        a job runs once, a manual: text once per leaf. A jenkins-credential: takes the value of the
        key whose entry names it, and a credential two entries name refuses the plan, as does a
        spec no step is built for."""
        steps: list[Step] = []
        kubernetes = False
        credentials: dict[str, Activation] = {}
        confirms: dict[tuple[str, str], None] = {}
        for activation in self.target.activations:
            leaf = activation.leaf
            for spec in activation.specs:
                if spec.name in KUBERNETES:
                    if not kubernetes:
                        steps += self._kubernetes()
                        kubernetes = True
                elif spec.name == "jenkins-job":
                    job = JenkinsJob(self.jenkins, *parse_job(spec.arg))
                    if job.id not in {step.id for step in steps}:
                        steps.append(job)
                elif spec.name == "jenkins-credential":
                    if other := credentials.get(spec.arg):
                        raise self._refused(
                            activation,
                            spec,
                            f"{self._entry(other)} names the credential too, and it takes one "
                            f"value",
                        )
                    credentials[spec.arg] = activation
                    steps.append(
                        JenkinsCredential(self.jenkins, spec.arg, kv=(leaf, activation.key))
                    )
                elif spec.name == "manual":
                    if (leaf, spec.arg) in confirms:
                        continue
                    confirms[leaf, spec.arg] = None
                    n = sum(1 for other, _ in confirms if other == leaf)
                    steps.append(
                        OperatorConfirm(f"{leaf}:{n}", spec.arg, f"for {leaf}", activator=True)
                    )
                else:
                    raise self._refused(activation, spec, "no step is built for it yet")
        return steps

    def _entry(self, activation: Activation) -> str:
        """The entry whose activate it is, in words: its leaf's own unless it is a copy's."""
        name = entry_name(activation.key)
        return name if activation.leaf == self.target.leaf else f"{activation.leaf}'s {name}"

    def _refused(self, activation: Activation, spec: Activator, why: str) -> PlanError:
        return PlanError(f"{self.target.leaf}: {self._entry(activation)} activate {spec}: {why}")

    def _cluster(self) -> Cluster:
        if self.cluster is None:
            raise PlanError(
                f"{self.target.leaf}: its activation is read from the cluster, which an offline "
                f"plan without a snapshot does not reach"
            )
        return self.cluster

    def _kubernetes(self) -> list[Step]:
        """The eso_sync_and_rollout of every written entry's eso and k8s-rollout specs. eso and a
        k8s-rollout without targets (auto is both) need an ExternalSecret that references the
        entry's leaf; a named rollout does not."""
        leaves: list[str] = []
        targets: list[Workload] = []
        for activation in self.target.activations:
            specs = [s for s in activation.specs if s.name in KUBERNETES]
            if not specs:
                continue
            leaf = activation.leaf
            found = self._external_secrets(leaf)
            if leaf not in leaves:
                leaves.append(leaf)
                self._consumed(f"{es.namespace}/externalsecret/{es.name}" for es in found)
            for spec in specs:
                if spec.targets:
                    targets += [Workload.parse(t) for t in spec.targets]
                    continue
                if not found:
                    raise self._refused(activation, spec, f"no ExternalSecret references {leaf}")
                if spec.name == "k8s-rollout":
                    derived = self._consumers(leaf)
                    targets += derived
                    self._consumed(str(w) for w in derived)
        return self.eso_sync_and_rollout(targets, leaves)

    def _external_secrets(self, leaf: str) -> list[Ref]:
        """The ExternalSecrets that reference the leaf."""
        if leaf not in self.derived.externalsecrets:
            self.derived.externalsecrets[leaf] = self._cluster().external_secrets(leaf)
        return self.derived.externalsecrets[leaf]

    def _consumers(self, leaf: str) -> list[Workload]:
        """auto's rollout targets for the leaf (Cluster.consumers)."""
        if leaf not in self.derived.workloads:
            self.derived.workloads[leaf] = self._cluster().consumers(leaf)
        return self.derived.workloads[leaf]

    def _consumed(self, items: Iterable[str]) -> None:
        self.consumers += [item for item in items if item not in self.consumers]


@dataclass(frozen=True)
class PlanContext:
    steps: StepFactory


class Kind(Protocol):
    """A kind's plugin (design §6, plugin contract), registered under its name. It may also have
    `credential(leaf: Target) -> str`, the key's credential in words for the UI's title (R85); the
    title of a kind without it shows the kind's name."""

    name: str  # the kind an entry names
    per_key: bool  # one plan per key of the kind on a leaf; else one plan of all of them

    def args_problems(self, args: Mapping) -> list[str]:
        """What is wrong with one key's args for this kind; empty when nothing is."""

    def ask(self, leaf: Target) -> str:
        """What the plan asks of the operator, in a few words; empty when it asks nothing."""

    def description(self, leaf: Target) -> str:
        """One or two sentences on the operator's part and the tool's."""

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        """The whole plan but kv.stamp, built with ctx.steps; the same steps with the same ids
        for the same Target."""


@dataclass(frozen=True)
class Plan:
    target: Target
    steps: tuple[Step, ...]
    ask: str = ""
    description: str = ""
    derived: Derived = field(default_factory=Derived)  # what it derived from the cluster

    @property
    def name(self) -> str:
        return f"{self.target.kind} plan of {self.target.leaf}"

    @property
    def needs_operator(self) -> bool:
        return any(step.actor is Actor.OPERATOR for step in self.steps)

    def index(self, step_id: str) -> int | None:
        return next((i for i, step in enumerate(self.steps) if step.id == step_id), None)


def target(leaf: str, kind: str, keys: list[str], audit: Audit) -> Target:
    """The Target of rotating these keys of the leaf, which must be of the kind and unblocked by
    the audit."""
    kinds = audit.kinds.get(leaf)
    if kinds is None:
        raise PlanError(f"{leaf}: no such leaf, or its keys cannot be read")
    if not is_scheduled(kind):
        raise PlanError(f"{leaf}: {kind} is not a kind the rotator rotates")
    problems = [] if keys else ["no key to rotate"]
    for key in sorted(keys):
        if kinds.get(key) != kind:
            problems.append(f"{key} is not a {kind} key")
        elif audit.blocked(leaf, key):
            problems.append(f"{key} is blocked by a finding")
    if problems:
        raise PlanError(f"{leaf}: {'; '.join(problems)}")
    copies = sorted(
        Copy(path, key, of[1])
        for path, leaf_kinds in audit.kinds.items()
        for key, k in leaf_kinds.items()
        if (of := copy_target(k)) and of[0] == leaf and of[1] in keys
    )
    # Unblocked, so every entry the plan writes is the audit's: a finding on a copy's entry, or on
    # a leaf a copy lands in, blocks the primary key.
    entries = audit.entries[leaf]
    written = sorted(copies, key=lambda c: (c.leaf != leaf, c.leaf, c.key))
    activations = (
        *(Activation(leaf, key, entries[key].activate) for key in sorted(keys)),
        *(Activation(c.leaf, c.key, audit.entries[c.leaf][c.key].activate) for c in written),
    )
    return Target(
        leaf,
        kind,
        tuple(sorted(keys)),
        {key: entries[key] for key in sorted(keys)},
        tuple(copies),
        activations,
    )


def build(
    kind: Kind,
    leaf: Target,
    cluster: Cluster | None = None,
    *,
    derived: Derived | None = None,
    jenkins: Jenkins | None = None,
    ansible: Ansible | None = None,
    ssh: Ssh | None = None,
) -> Plan:
    """The kind's plan of the Target; derived: a plan in flight's record of what it derived."""
    problems = [
        f"{entry_name(key)} args: {problem}"
        for key in leaf.keys
        for problem in kind.args_problems(leaf.entries[key].args)
    ]
    if problems:
        raise PlanError(f"{leaf.leaf}: {'; '.join(problems)}")
    factory = StepFactory(leaf, cluster, derived=derived, jenkins=jenkins, ansible=ansible, ssh=ssh)
    planned = kind.plan(leaf, PlanContext(factory))
    steps = [*planned, KvStamp(leaf.leaf, leaf.keys, tuple(factory.consumers))]
    ids = [step.id for step in steps]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise PlanError(f"{leaf.leaf}: the {kind.name} plan repeats step id(s) {', '.join(dupes)}")
    return Plan(leaf, tuple(steps), kind.ask(leaf), kind.description(leaf), factory.derived)


def make(
    kinds: Mapping[str, Kind],
    leaf: str,
    kind: str,
    keys: list[str],
    audit: Audit,
    cluster: Cluster | None = None,
    *,
    derived: Derived | None = None,
    jenkins: Jenkins | None = None,
    ansible: Ansible | None = None,
    ssh: Ssh | None = None,
) -> Plan:
    """The plan of rotating these keys of the leaf, built by the kind's plugin; derived: a plan
    in flight's record of what it derived from the cluster."""
    if kind not in kinds:
        raise PlanError(f"{leaf}: {kind} is not a kind this install has a plugin for")
    built = target(leaf, kind, keys, audit)
    return build(
        kinds[kind], built, cluster, derived=derived, jenkins=jenkins, ansible=ansible, ssh=ssh
    )


def in_flight(
    kinds: Mapping[str, Kind],
    leaf: str,
    flight: InFlight,
    audit: Audit,
    cluster: Cluster | None = None,
) -> Plan:
    """The leaf's plan in flight, built again from its entries and what it derived from the
    cluster when it started (design §4.5)."""
    return make(kinds, leaf, flight.kind, list(flight.keys), audit, cluster, derived=flight.derived)


def split(kind: Kind, keys: Iterable[str]) -> list[tuple[str, ...]]:
    """The key sets of a kind's plans on one leaf: one per key for a per-key kind, else one."""
    ordered = tuple(sorted(keys))
    return [(key,) for key in ordered] if kind.per_key else [ordered]


@dataclass(frozen=True)
class LeafPlan:
    kind: str
    keys: tuple[str, ...]
    due_at: datetime.date | None  # the earliest of its keys' (schedule.KeySchedule); None: never
    plan: Plan | None  # None: it cannot be built, for error
    error: str = ""


def _blocked_by(audit: Audit, leaf: str, key: str) -> str:
    found = [
        f"{f.key}: {f.message}"
        for f in audit.findings
        if f.leaf == leaf and f.blocks in (None, key)
    ]
    return "; ".join(found) or "a leaf it is copied into has a finding"


def of_leaf(
    leaf: str,
    store: Mapping[str, Leaf],
    audit: Audit,
    kinds: Mapping[str, Kind],
    cluster: Cluster | None = None,
) -> tuple[list[LeafPlan], dict[str, str]]:
    """The leaf's plans by hand, the soonest due first: each covers every key of its kind on the
    leaf, per key for a per-key kind; and each other key with why it has none."""
    leaf_kinds = audit.kinds.get(leaf, {})
    groups: dict[str, list[str]] = {}
    unplanned = {}
    for key, kind in sorted(leaf_kinds.items()):
        if audit.blocked(leaf, key):
            unplanned[key] = f"blocked: {_blocked_by(audit, leaf, key)}"
        elif kind == NONE:
            unplanned[key] = "none: not a secret, never rotated"
        elif of := copy_target(kind):
            unplanned[key] = f"a copy of {of[0]}#{of[1]}, written by its primary's plan"
        elif kind not in kinds:
            unplanned[key] = f"{kind}: a kind this install has no plugin for yet"
        else:
            groups.setdefault(kind, []).append(key)
    if not groups:
        return [], unplanned
    planned = {key: audit.entries[leaf][key] for keys in groups.values() for key in keys}
    due = {s.key: s.due_at for s in schedule(leaf, planned, store[leaf].state.stamps)}
    plans = []
    for kind, keys in groups.items():
        for subset in split(kinds[kind], keys):
            dates = [due[key] for key in subset if due[key] is not None]
            try:
                built = make(kinds, leaf, kind, list(subset), audit, cluster)
                error = ""
            except PlanError as e:
                built, error = None, str(e)
            plans.append(LeafPlan(kind, subset, min(dates, default=None), built, error))
    never = datetime.date.max
    plans.sort(key=lambda p: (p.due_at is None, p.due_at or never, p.kind, p.keys))
    return plans, unplanned
