"""secret-rotator: the commands of design §8."""

import argparse
import functools
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from secret_rotator import annotate as ann
from secret_rotator import audit as aud
from secret_rotator import nightly, registry, terminal
from secret_rotator.cluster import Cluster
from secret_rotator.console import Console
from secret_rotator.kube import Kube, KubeError
from secret_rotator.lock import holder_name, utcnow
from secret_rotator.openbao import OpenBao, OpenBaoError
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
SECRET_ROTATOR_ROLE_ID and SECRET_ROTATOR_SECRET_ID; but annotate, they read the prd
cluster with the ServiceAccount token in SECRET_ROTATOR_K8S_TOKEN. No output carries a
secret value. Exit status: 0 on success, 1 on a finding or a failure, 2 on a usage error."""


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
        description="Lists, per leaf, the metadata keys the seed adds or changes; --apply writes "
        "them by metadata patch, never put, and creates the marker leaves the seed declares.",
    )
    annotate.add_argument("--seed", type=Path, help="the seed (default: the packaged one)")
    annotate.add_argument("--apply", action="store_true", help="write what the dry run lists")
    plan = commands.add_parser(
        "plan",
        help="print a leaf's plans and execute nothing",
        description="Prints each plan of the leaf — one per kind, one per key for manual — with "
        "when it falls due and every step with its target, then why each other key has none.",
    )
    plan.add_argument("path", help="the leaf, a path of the kv mount")
    plan.add_argument(
        "--keys",
        type=Path,
        help="offline: plan from the seed over FILE's key names instead of the store",
        metavar="FILE",
    )
    plan.add_argument("--seed", type=Path, help="with --keys: the seed (default: the packaged one)")
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
    return p


def connect(
    environ: Mapping[str, str], opener: Callable | None, clock: Callable[[], float]
) -> OpenBao:
    bao = OpenBao(opener=opener, clock=clock)
    bao.login_approle(environ[ROLE_ID_ENV], environ[SECRET_ID_ENV])
    bao.credentials = functools.partial(own_credentials, bao)
    return bao


def own_credentials(bao: OpenBao) -> tuple[str, str]:
    """The rotator's AppRole as the store holds it now."""
    return bao.value(OWN_LEAF, "role_id"), bao.value(OWN_LEAF, "secret_id")


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
) -> int:
    p = parser()
    args = p.parse_args(argv)
    offline = args.command in ("audit", "plan") and args.keys is not None
    if args.command in ("audit", "plan") and args.seed and not offline:
        p.error(f"the live {args.command} reads the store, not a seed: --seed goes with --keys")
    if not offline and not (environ.get(ROLE_ID_ENV) and environ.get(SECRET_ID_ENV)):
        p.error(
            f"{ROLE_ID_ENV} and {SECRET_ID_ENV} are not set: the rotator's AppRole, "
            f"kv/iac/rotator-approle"
        )
    reads_cluster = not offline and args.command != "annotate"
    if reads_cluster and not environ.get(K8S_TOKEN_ENV):
        p.error(
            f"{K8S_TOKEN_ENV} is not set: the secret-rotator ServiceAccount's token, "
            f"kv/iac/rotator-k8s-token"
        )
    seed_path = args.seed or ann.DEFAULT_SEED
    today = utcnow().date()
    try:
        if args.command == "run" and args.path is None:
            return run_nightly(environ, opener, out, kube, switches(), youtrack, telegram, clock)
        kinds = registry.load() if args.command in ("plan", "run") else {}
        if offline:
            store = ann.offline_store(args.keys, ann.load_seed(seed_path), out)
            if args.command == "plan":
                return terminal.print_leaf(out, args.path, store, aud.audit(store), kinds, today)
            return aud.report(aud.audit(store), store, out)
        seed = ann.load_seed(seed_path) if args.command == "annotate" else None
        bao = connect(environ, opener, clock)
        if seed is not None:
            return ann.run_apply(bao, seed, args.apply, out)
        cluster = Cluster(kube(environ[K8S_TOKEN_ENV]))
        if args.command == "run":
            con = console()
            return terminal.run_leaf(
                bao,
                args.path,
                kinds,
                con,
                holder=holder_name(f"run {args.path}"),
                today=today,
                cluster=cluster,
                notify=notifier(bao, switches().telegram_chat_id, telegram, con),
            )
        store = aud.live_store(bao)
        result = aud.audit(store, cluster.referenced())
        if args.command == "plan":
            return terminal.print_leaf(out, args.path, store, result, kinds, today, cluster)
        return aud.report(result, store, out)
    except (
        ann.SeedError,
        registry.RegistryError,
        SwitchesError,
        OpenBaoError,
        KubeError,
        OSError,
    ) as e:
        out(f"error: {e}")
        return 1


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
    """`run` without a path. paused stops it before it does anything (design §8)."""
    if switches.paused:
        out("paused: the switches stop the nightly run before it does anything")
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
