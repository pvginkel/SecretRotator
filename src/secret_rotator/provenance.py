"""The SecretRotator commit a run logs. The iac image installs SecretRotator from its prd branch
with a VCS install, which records the commit it installed in the distribution's direct_url.json
(PEP 610)."""

import json
from importlib import metadata

DISTRIBUTION = "secret-rotator"


def describe(record: str | None) -> str:
    """`commit <sha>` from a VCS install's direct_url.json; else where the install came from."""
    if record is None:
        return "no install record"
    direct = json.loads(record)
    if "vcs_info" in direct:
        return f"commit {direct['vcs_info']['commit_id']}"
    return f"{direct['url']}, not a VCS install"


def source() -> str:
    """What this process runs, as describe() tells it."""
    return describe(metadata.distribution(DISTRIBUTION).read_text("direct_url.json"))
