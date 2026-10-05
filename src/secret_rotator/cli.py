"""secret-rotator: the commands of design §8."""

import argparse
import functools
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from secret_rotator import annotate as ann
from secret_rotator import audit as aud
from secret_rotator.openbao import OpenBao, OpenBaoError

# The rotator's AppRole, kv/iac/rotator-approle, which iac-impl puts in the iac container's
# environment.
ROLE_ID_ENV = "SECRET_ROTATOR_ROLE_ID"
SECRET_ID_ENV = "SECRET_ROTATOR_SECRET_ID"

PRINT = functools.partial(print, flush=True)

DESCRIPTION = """\
Rotates the secrets of OpenBao's kv mount by their annotations (AnsibleSpecs
secret-rotation/design.md). Live commands log in with the rotator's AppRole from
SECRET_ROTATOR_ROLE_ID and SECRET_ROTATOR_SECRET_ID. No output carries a secret value.
Exit status: 0 on success, 1 on a finding or a failure, 2 on a usage error."""


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
) -> int:
    p = parser()
    args = p.parse_args(argv)
    offline = args.command == "audit" and args.keys is not None
    if args.command == "audit" and args.seed and not offline:
        p.error("the live audit reads the store, not a seed: --seed goes with --keys")
    if not offline and not (environ.get(ROLE_ID_ENV) and environ.get(SECRET_ID_ENV)):
        p.error(
            f"{ROLE_ID_ENV} and {SECRET_ID_ENV} are not set: the rotator's AppRole, "
            f"kv/iac/rotator-approle"
        )
    seed_path = args.seed or ann.DEFAULT_SEED
    try:
        if offline:
            store = ann.offline_store(args.keys, ann.load_seed(seed_path), out)
            return aud.report(aud.audit(store), store, out)
        seed = ann.load_seed(seed_path) if args.command == "annotate" else None
        bao = connect(environ, opener)
        if seed is None:
            store = aud.live_store(bao)
            return aud.report(aud.audit(store), store, out)
        return ann.run_apply(bao, seed, args.apply, out)
    except (ann.SeedError, OpenBaoError, OSError) as e:
        out(f"error: {e}")
        return 1
