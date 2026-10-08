"""secret-rotator: the commands of design §8."""

import argparse
import datetime
import functools
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from secret_rotator import annotate as ann
from secret_rotator import audit as aud
from secret_rotator import metrics, nightly, provenance, registry, terminal, ui
from secret_rotator.cluster import SNAPSHOT, Cluster, SnapshotError
from secret_rotator.console import Console
from secret_rotator.contract import (
    ContractError,
    dump_entry,
    entry_name,
    is_scheduled,
    kind_in,
    load_entry,
    parse_date,
)
from secret_rotator.kube import Kube, KubeError
from secret_rotator.lock import holder_name, utcnow
from secret_rotator.openbao import OpenBao, OpenBaoError
from secret_rotator.state import LeafState, State
from secret_rotator.switches import Switches, SwitchesError
from secret_rotator.switches import load as load_switches
from secret_rotator.telegram import TOKEN as BOT_TOKEN
from secret_rotator.telegram import Telegram, TelegramError
from secret_rotator.youtrack import YouTrack

# The rotator's AppRole, kv/iac/rotator-approle, which iac-impl puts in the iac container's
# environment. A run logs in again from the leaf itself: once the rotator has rotated it, the
# environment's secret_id is destroyed.
OWN_LEAF = "iac/rotator-approle"
ROLE_ID_ENV = "SECRET_ROTATOR_ROLE_ID"
SECRET_ID_ENV = "SECRET_ROTATOR_SECRET_ID"
# The secret-rotator ServiceAccount's token, kv/iac/rotator-k8s-token, put there the same way.
K8S_TOKEN_ENV = "SECRET_ROTATOR_K8S_TOKEN"

PRINT = functools.partial(print, flush=True)

DESCRIPTION = """\
Rotates the secrets of OpenBao's kv mount by their annotations (AnsibleSpecs
secret-rotation/design.md). Live commands log in with the rotator's AppRole from
SECRET_ROTATOR_ROLE_ID and SECRET_ROTATOR_SECRET_ID; but annotate and stamp, they read the
prd cluster with the ServiceAccount token in SECRET_ROTATOR_K8S_TOKEN. run and stamp push their
metrics to the Pushgateway with that token; stamp without it stamps all the same. No output
carries a secret value. Exit status: 0 on success, 1 on a finding or a failure, 2 on a usage
error."""


def iso_date(text: str) -> datetime.date:
    try:
        return parse_date(text)
    except ContractError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="secret-rotator",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = p.add_subparsers(dest="command", required=True, metavar="command")
    audit = commands.add_parser(
        "audit",
        help="check every leaf of the kv mount against the annotation contract",
        description="Prints one line per finding, <leaf>: <key>: <what>, then every key that "
        "never rotates. Reads key names, never values.",
    )
    audit.add_argument(
        "--keys",
        type=Path,
        help="offline: check the seed over FILE's key names (a JSON object of leaf path -> "
        "key names) instead of the store",
        metavar="FILE",
    )
    audit.add_argument(
        "--seed", type=Path, help="with --keys: the seed (default: the packaged one)"
    )
    annotate = commands.add_parser(
        "annotate",
        help="write the seed's annotations onto the kv mount (a dry run without --apply)",
        description="Lists, per leaf, the metadata keys it adds, changes and removes to make the "
        "leaf's custom metadata exactly the seed's entries, and an automatic leaf's max_versions "
        "it sets to 20; --apply writes them by metadata patch, never put, and creates the marker "
        "leaves the seed declares.",
    )
    annotate.add_argument("--seed", type=Path, help="the seed (default: the packaged one)")
    annotate.add_argument("--apply", action="store_true", help="write what the dry run lists")
    plan = commands.add_parser(
        "plan",
        help="print a leaf's plans and execute nothing",
        description="Prints each plan of the leaf — one per kind, one per key for manual and "
        "external — with when it falls due and every step with its target, then why each other "
        "key has none.",
    )
    plan.add_argument("path", help="the leaf, a path of the kv mount")
    plan.add_argument(
        "--keys",
        type=Path,
        help="offline: plan from the seed over FILE's key names instead of the store",
        metavar="FILE",
    )
    plan.add_argument("--seed", type=Path, help="with --keys: the seed (default: the packaged one)")
    plan.add_argument(
        "--snapshot",
        type=Path,
        help="with --keys: derive the syncs and rollout targets from FILE, a read-only snapshot of "
        f"the prd cluster: what `kubectl get {SNAPSHOT} -A -o json` prints. Without it, an "
        "offline plan of a leaf activated through the cluster cannot be built",
        metavar="FILE",
    )
    run = commands.add_parser(
        "run",
        help="without a path, the nightly run; with one, run a plan of that leaf in the terminal",
        description="Without a path, the nightly run: compliance on the standing card, every due "
        "plan with no operator step run under the switches, a failed one rolled back, the digest "
        "and every failure in Telegram. With a path, takes up the leaf's plan in flight, else runs "
        "the plan picked from its plans, its operator steps as prompts. A value is entered at a "
        "hidden prompt, never on the command line.",
    )
    run.add_argument("path", nargs="?", help="the leaf, a path of the kv mount")
    run.set_defaults(keys=None, seed=None)
    stamp = commands.add_parser(
        "stamp",
        help="set a key's rotation stamp in the run state, or set or clear its expires_at",
        description="Sets the key's rotation stamp in kv/rotator/state to the date its current "
        "value was written: the key falls due its interval after that date. For a value written "
        "outside a rotation, as the go-live writes the rotator's own leaves. Sets or clears the "
        "expires_at in the key's rotation_<key> entry, the date its current credential stops "
        "working: the key falls due 7 days before it. Once the entry exists, only a rotation and "
        "this command write its expires_at. Then pushes the run state's metrics.",
    )
    stamp.add_argument("path", help="the leaf, a path of the kv mount")
    stamp.add_argument("key", help="a data key of the leaf")
    stamp.add_argument(
        "--rotated-at",
        type=iso_date,
        help="the date the key's current value was written, not after today",
        metavar="YYYY-MM-DD",
    )
    expiry = stamp.add_mutually_exclusive_group()
    expiry.add_argument(
        "--expires-at",
        type=iso_date,
        help="the date the key's current credential stops working",
        metavar="YYYY-MM-DD",
    )
    expiry.add_argument(
        "--clear-expires-at",
        action="store_true",
        help="the key's current credential does not expire",
    )
    stamp.set_defaults(keys=None, seed=None)
    ui_command = commands.add_parser(
        "ui",
        help="the operator's terminal UI over every rotation with a step of theirs",
        description="Lists every plan with an operator step as a box: the plans in flight and "
        "failed first, then by when they fall due, the earliest first, then those without a due "
        "date.",
    )
    ui_command.set_defaults(keys=None, seed=None)
    return p


def connect(
    environ: Mapping[str, str], opener: Callable | None, clock: Callable[[], float]
) -> OpenBao:
    bao = OpenBao(opener=opener, clock=clock)
    bao.login_approle(environ[ROLE_ID_ENV], environ[SECRET_ID_ENV])
    bao.credential_leaf = OWN_LEAF
    return bao


def notifier(
    bao: OpenBao,
    chat: int | None,
    telegram: Callable[[str, int], Telegram],
    console: Console,
) -> Callable[[str], None] | None:
    """What tells `run <path>`'s failures in Telegram; None until a chat id is committed."""
    if chat is None:
        return None

    def notify(text: str) -> None:
        try:
            telegram(bao.value(*BOT_TOKEN), chat).send(text)
        except (OpenBaoError, TelegramError) as e:
            console.line(f"The Telegram message about it is not sent: {e}")

    return notify


def main(
    argv: list[str] | None = None,
    *,
    opener: Callable | None = None,
    out: Callable[[str], None] = PRINT,
    environ: Mapping[str, str] = os.environ,
    console: Callable[[], Console] = Console,
    kube: Callable[[str], Kube] = Kube,
    switches: Callable[[], Switches] = load_switches,
    youtrack: Callable[[str], YouTrack] = YouTrack,
    telegram: Callable[[str, int], Telegram] = Telegram,
    clock: Callable[[], float] = time.monotonic,
    source: Callable[[], str] = provenance.source,
) -> int:
    p = parser()
    args = p.parse_args(argv)
    if args.command == "run":
        # Before anything that can fail: a run that fails at its start-up has named its commit.
        what = "run" if args.path is None else f"run {args.path}"
        out(f"secret-rotator {what}, {source()}")
    offline = args.command in ("audit", "plan") and args.keys is not None
    if args.command in ("audit", "plan") and args.seed and not offline:
        p.error(f"the live {args.command} reads the store, not a seed: --seed goes with --keys")
    if args.command == "plan" and args.snapshot and not offline:
        p.error("the live plan reads the cluster, not a snapshot: --snapshot goes with --keys")
    if not offline and not (environ.get(ROLE_ID_ENV) and environ.get(SECRET_ID_ENV)):
        p.error(
            f"{ROLE_ID_ENV} and {SECRET_ID_ENV} are not set: the rotator's AppRole, "
            f"kv/iac/rotator-approle"
        )
    reads_cluster = not offline and args.command not in ("annotate", "stamp")
    if reads_cluster and not environ.get(K8S_TOKEN_ENV):
        p.error(
            f"{K8S_TOKEN_ENV} is not set: the secret-rotator ServiceAccount's token, "
            f"kv/iac/rotator-k8s-token"
        )
    seed_path = args.seed or ann.DEFAULT_SEED
    today = utcnow().date()
    if args.command == "stamp":
        if not (args.rotated_at or args.expires_at or args.clear_expires_at):
            p.error("stamp sets something: --rotated-at, --expires-at or --clear-expires-at")
        if args.rotated_at and args.rotated_at > today:
            p.error(f"--rotated-at {args.rotated_at} is after today, {today}")
    try:
        if args.command == "run" and args.path is None:
            return run_nightly(environ, opener, out, kube, switches(), youtrack, telegram, clock)
        kinds = registry.load() if args.command in ("audit", "plan", "run", "stamp", "ui") else {}
        if offline:
            store = ann.offline_store(args.keys, ann.load_seed(seed_path), out)
            if args.command == "plan":
                cluster = None if args.snapshot is None else Cluster.of_snapshot(args.snapshot)
                referenced = None if cluster is None else cluster.referenced()
                result = aud.audit(store, referenced, kinds)
                return terminal.print_leaf(out, args.path, store, result, kinds, today, cluster)
            return aud.report(aud.audit(store, plugins=kinds), store, out)
        seed = ann.load_seed(seed_path) if args.command == "annotate" else None
        bao = connect(environ, opener, clock)
        if seed is not None:
            return ann.run_apply(bao, seed, args.apply, out)
        if args.command == "stamp":
            code = run_stamp(
                bao,
                args.path,
                args.key,
                out,
                rotated_at=args.rotated_at,
                expires_at=args.expires_at,
                clear_expiry=args.clear_expires_at,
            )
            if code == 0:
                if environ.get(K8S_TOKEN_ENV):
                    metrics.push_state(bao, kinds, kube(environ[K8S_TOKEN_ENV]), out)
                else:
                    out(f"metrics: the state group is not pushed: {K8S_TOKEN_ENV} is not set")
            return code
        cluster = Cluster(kube(environ[K8S_TOKEN_ENV]))
        if args.command == "ui":
            return ui.main(bao, kinds, cluster, today)
        if args.command == "run":
            con = console()
            code = terminal.run_leaf(
                bao,
                args.path,
                kinds,
                con,
                holder=holder_name(f"run {args.path}"),
                today=today,
                cluster=cluster,
                notify=notifier(bao, switches().telegram_chat_id, telegram, con),
            )
            metrics.push_state(bao, kinds, cluster.kube, out)
            return code
        store = aud.live_store(bao, runs=args.command == "plan")
        result = aud.audit(store, cluster.referenced(), kinds)
        if args.command == "plan":
            return terminal.print_leaf(out, args.path, store, result, kinds, today, cluster)
        return aud.report(result, store, out)
    except (
        ann.SeedError,
        SnapshotError,
        registry.RegistryError,
        SwitchesError,
        OpenBaoError,
        KubeError,
        OSError,
    ) as e:
        out(f"error: {e}")
        return 1


def run_stamp(
    bao: OpenBao,
    leaf: str,
    key: str,
    out: Callable[[str], None],
    *,
    rotated_at: datetime.date | None = None,
    expires_at: datetime.date | None = None,
    clear_expiry: bool = False,
) -> int:
    """`stamp`: the key's rotation stamp set in the run state, given rotated_at; then the
    expires_at in its entry set, given expires_at, or cleared. 1, before anything is written,
    when the store has no such leaf or key, or the key's entry takes no expires_at."""
    leaves = bao.leaves()
    if leaf not in leaves:
        out(f"error: no leaf {leaf}")
        return 1
    if key not in (bao.subkeys(leaf) or ()):
        out(f"error: no key {key} in the current version of {leaf}")
        return 1
    expiry = expires_at is not None or clear_expiry
    name = entry_name(key)
    entry = (bao.metadata(leaf) or {}).get(name) if expiry else None
    if expiry and entry is None:
        out(f"error: {leaf} has no entry {name}")
        return 1
    if expiry and not is_scheduled(kind_in(entry)):
        out(f"error: {leaf}#{key} is no scheduled key: its entry takes no expires_at")
        return 1
    if rotated_at is not None:
        was: dict[str, str | None] = {}

        def stamp(state: LeafState) -> None:
            was[key] = state.stamps.get(key)
            state.stamps[key] = rotated_at.isoformat()

        State(bao, leaves).update(leaf, stamp)
        out(f"{leaf}#{key}: rotation stamp {rotated_at}, was {was[key] or 'none'}")
    if expiry:
        fields = load_entry(entry)
        before = fields.pop("expires_at", None)
        if expires_at is not None:
            fields["expires_at"] = expires_at.isoformat()
        if fields.get("expires_at") != before:
            bao.patch_metadata(leaf, {name: dump_entry(fields)})
        out(f"{leaf}#{key}: expires_at {expires_at or 'none'}, was {before or 'none'}")
    return 0


def run_nightly(
    environ: Mapping[str, str],
    opener: Callable | None,
    out: Callable[[str], None],
    kube: Callable[[str], Kube],
    switches: Switches,
    youtrack: Callable[[str], YouTrack],
    telegram: Callable[[str, int], Telegram],
    clock: Callable[[], float],
) -> int:
    """`run` without a path. paused stops it before it does anything (design §8) but push its run
    health, with secret_rotator_paused 1 (design §3.4)."""
    if switches.paused:
        began = clock()
        out("paused: the switches stop the nightly run before it does anything")
        health = metrics.run_health(
            ended=utcnow(),
            duration=clock() - began,
            success=True,
            rotations=0,
            deferred=0,
            switches=switches,
        )
        groups = {metrics.NIGHTLY: lambda: health}
        if pushed := metrics.push(kube(environ[K8S_TOKEN_ENV]), groups, out):
            out(f"metrics: pushed {', '.join(pushed)}")
        return 0
    bao = connect(environ, opener, clock)
    return nightly.run(
        bao,
        Cluster(kube(environ[K8S_TOKEN_ENV])),
        registry.load(),
        switches,
        youtrack=youtrack,
        telegram=telegram,
        out=out,
        holder=holder_name("run"),
        clock=clock,
    )
