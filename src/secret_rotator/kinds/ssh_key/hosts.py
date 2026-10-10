"""The hosts that hold the Ansible SSH key, as the Ansible repo writes them down once (Ansible slice
060's ruling D2): the prd inventory's groups ansible_key_always_up, reached over SSH, and
ansible_key_may_be_off, the VMs that may be off, reached through their PVE node's guest agent. The
rotation playbook plays the same groups; where a VM that may be off runs is never read here (A1)."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from secret_rotator.ansiblesteps import ANSIBLE_DIR

INVENTORY = ANSIBLE_DIR / "inventories" / "prd" / "hosts.yml"
ALWAYS_UP = "ansible_key_always_up"
MAY_BE_OFF = "ansible_key_may_be_off"


class InventoryError(Exception):
    pass


@dataclass(frozen=True)
class Hosts:
    always_up: tuple[str, ...]  # sorted
    may_be_off: tuple[str, ...]  # sorted, by their PVE names


def _groups(tree: object) -> dict[str, tuple[set[str], set[str]]]:
    """Each group of a YAML inventory under all: its own hosts and its children, every place that
    names it merged."""
    groups: dict[str, tuple[set[str], set[str]]] = {}

    def walk(name: str, node: object) -> None:
        hosts, children = groups.setdefault(name, (set(), set()))
        if node is None:
            return
        if not isinstance(node, Mapping):
            raise InventoryError(f"group {name} is not a mapping")
        hosts.update(node.get("hosts") or {})
        for child, sub in (node.get("children") or {}).items():
            children.add(child)
            walk(child, sub)

    if not isinstance(tree, Mapping) or "all" not in tree:
        raise InventoryError("it has no group all")
    walk("all", tree["all"])
    return groups


def _resolve(groups: dict[str, tuple[set[str], set[str]]], name: str) -> set[str]:
    """The group's hosts and those of its children, theirs included."""
    found: set[str] = set()
    seen: set[str] = set()
    todo = [name]
    while todo:
        group = todo.pop()
        if group in seen:
            continue
        seen.add(group)
        hosts, children = groups[group]
        found |= hosts
        todo += children
    return found


def hosts(inventory: Path = INVENTORY) -> Hosts:
    """The two classes of the hosts that hold the key, as the inventory's groups resolve. Raises
    InventoryError naming what is wrong."""
    try:
        tree = yaml.safe_load(inventory.read_text(encoding="utf-8"))
    except OSError as e:
        raise InventoryError(
            f"the Ansible inventory {inventory} cannot be read: {e.strerror}"
        ) from None
    except yaml.YAMLError as e:
        raise InventoryError(f"the Ansible inventory {inventory} is no YAML: {e}") from None
    try:
        groups = _groups(tree)
    except InventoryError as e:
        raise InventoryError(f"the Ansible inventory {inventory}: {e}") from None
    if missing := [g for g in (ALWAYS_UP, MAY_BE_OFF) if g not in groups]:
        raise InventoryError(
            f"the Ansible inventory {inventory} has no group {' or '.join(missing)}"
        )
    return Hosts(
        tuple(sorted(_resolve(groups, ALWAYS_UP))), tuple(sorted(_resolve(groups, MAY_BE_OFF)))
    )
