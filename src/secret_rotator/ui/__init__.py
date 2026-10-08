"""`secret-rotator ui` (design §7): the operator's terminal UI over every rotation with a step of
theirs."""

import datetime
import os
from collections.abc import Mapping, MutableMapping

from secret_rotator.audit import live_store
from secret_rotator.cluster import Cluster
from secret_rotator.listing import listed
from secret_rotator.openbao import OpenBao
from secret_rotator.plan import Kind
from secret_rotator.ui.app import RotatorApp


def full_colour(environ: MutableMapping[str, str]) -> None:
    """Truecolor unless the environment names a colour system: the VS Code task's chain hands the
    app TERM=xterm and no COLORTERM, from which Textual draws 16 colours (R95). Textual reads
    os.environ when the app is constructed."""
    if "COLORTERM" not in environ and "TEXTUAL_COLOR_SYSTEM" not in environ:
        environ["COLORTERM"] = "truecolor"


def main(bao: OpenBao, kinds: Mapping[str, Kind], cluster: Cluster, today: datetime.date) -> int:
    rotations = listed(live_store(bao, runs=True), kinds, cluster)
    full_colour(os.environ)
    RotatorApp(rotations, today=today, now=datetime.datetime.now().astimezone()).run()
    return 0
