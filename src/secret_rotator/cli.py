"""secret-rotator: the commands of design §8."""

import argparse
import functools
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from secret_rotator import annotate as ann
from secret_rotator import audit as aud
from secret_rotator import registry, terminal
from secret_rotator.cluster import Cluster
from secret_rotator.console import Console
from secret_rotator.kube import Kube, KubeError
from secret_rotator.lock import holder_name, utcnow
from secret_rotator.openbao import OpenBao, OpenBaoError

# The rotator's AppRole, kv/iac/rotator-approle, which iac-impl puts in the iac container's
# environment.
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
        help="run a plan of a leaf in the terminal, its operator steps as prompts",
        description="Takes up the leaf's plan in flight, else runs the plan picked from its plans. "
        "A value is entered at a hidden prompt, never on the command line.",
    )
    run.add_argument("path", help="the leaf, a path of the kv mount")
    run.set_defaults(keys=None, seed=None)
    return p


def connect(environ: Mapping[str, str], opener: Callable | None) -> OpenBao:
    bao = OpenBao(opener=opener)
    bao.login_approle(environ[ROLE_ID_ENV], environ[SECRET_ID_ENV])
    return bao


def main(
    argv: list[str] | None = None,
    *,
    opener: Callable | None = None,
    out: Callable[[str], None] = PRINT,
    environ: Mapping[str, str] = os.environ,
    console: Callable[[], Console] = Console,
    kube: Callable[[str], Kube] = Kube,
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
        kinds = registry.load() if args.command in ("plan", "run") else {}
        if offline:
            store = ann.offline_store(args.keys, ann.load_seed(seed_path), out)
            if args.command == "plan":
                return terminal.print_leaf(out, args.path, store, aud.audit(store), kinds, today)
            return aud.report(aud.audit(store), store, out)
        seed = ann.load_seed(seed_path) if args.command == "annotate" else None
        bao = connect(environ, opener)
        if seed is not None:
            return ann.run_apply(bao, seed, args.apply, out)
        cluster = Cluster(kube(environ[K8S_TOKEN_ENV]))
        if args.command == "run":
            holder = holder_name(f"run {args.path}")
            return terminal.run_leaf(
                bao, args.path, kinds, console(), holder=holder, today=today, cluster=cluster
            )
        store = aud.live_store(bao)
        result = aud.audit(store, cluster.referenced())
        if args.command == "plan":
            return terminal.print_leaf(out, args.path, store, result, kinds, today, cluster)
        return aud.report(result, store, out)
    except (ann.SeedError, registry.RegistryError, OpenBaoError, KubeError, OSError) as e:
        out(f"error: {e}")
        return 1
