"""The kinds this install implements: one package each, found through the secret_rotator.kinds
entry points (design §6, plugin contract). The core never imports a kind's package itself; none
and copies are no plugins, but the core's (design §3.1)."""

from importlib.metadata import EntryPoint, entry_points

from secret_rotator.contract import KINDS
from secret_rotator.plan import Kind

GROUP = "secret_rotator.kinds"


class RegistryError(Exception):
    pass


def load(found: list[EntryPoint] | None = None) -> dict[str, Kind]:
    """Each kind's plugin by its name; RegistryError for a plugin that is not one kind of design
    §6, named as it registered."""
    kinds: dict[str, Kind] = {}
    for ep in entry_points(group=GROUP) if found is None else found:
        if ep.name not in KINDS:
            raise RegistryError(f"{ep.value}: {ep.name!r} is not a kind of design §6")
        if ep.name in kinds:
            raise RegistryError(f"{ep.value}: {ep.name} has a plugin already")
        kind = ep.load()()
        if kind.name != ep.name:
            raise RegistryError(f"{ep.value}: registered as {ep.name}, named {kind.name}")
        kinds[ep.name] = kind
    return kinds
