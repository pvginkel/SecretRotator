"""The ssh-key kind (design §6, §4.6; Ansible slice 060): the ansible user's SSH key, the leaf's one
key, an OpenSSH private key. Its plan, after the core's start of each VM that may be off among the
hosts that hold the key (vmsteps), checks that every host reached over SSH answers, generates a new
ed25519 keypair (ssh.keygen) and runs Ansible's rotate-ansible-key.yml, which authorises the new
public half on every host and logs in to each with the new private half while the rotator still
logs in with the old key. It then commits the public half to the Ansible repo's ansible.pub
(github.commit), writes the leaf and its copies, from which on the rotator logs in with the new key
(ansiblekey), switches srviac's copy of the key (ssh.key_copy), activates, and runs the playbook
again, logging in with the new key, to take exactly the old public half off every host. Every
mutating step has an undo, so a failed plan rolls back in reverse; with no operator step it runs
at night."""

from collections.abc import Callable, Mapping
from pathlib import Path

from secret_rotator.ansiblesteps import Playbook
from secret_rotator.kinds.ssh_key.hosts import INVENTORY, Hosts, InventoryError, hosts
from secret_rotator.kinds.ssh_key.steps import Reach
from secret_rotator.model import Context, Step, StepFailed, value_name
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part
from secret_rotator.sshsteps import old_name, old_public_name, public_name
from secret_rotator.vmsteps import location_name

COMMENT = "ansible"  # the public half's comment, as ansible.pub holds it
PLAYBOOK = "playbooks/rotate-ansible-key.yml"
REPO = "pvginkel/Ansible"
# The committed public half: the bootstrap role installs it, and Terraform renders it into each
# from-scratch VM's cloud-init.
PUBLIC = "ansible/roles/bootstrap/files/ansible.pub"
CREDENTIALS = "rotator/ssh-key/credentials"  # key token: Contents read and write on REPO
MESSAGE = "rotate the ansible user's SSH key"


def book(found: Hosts, state: str, public: str, private: str | None = None) -> Playbook:
    """A run of the rotation playbook: state present or absent of the public half staged under
    that name, present with the private half it proves, and where each VM that may be off runs."""
    staged = {"key_rotation_public": public}
    if private is not None:
        staged["key_rotation_private"] = private
    staged |= {f"key_rotation_vm_{vm}": location_name(vm) for vm in found.may_be_off}
    return Playbook(PLAYBOOK, staged=staged, extra={"key_rotation_state": state})


def committed(key: str) -> Callable[[Context, str], str]:
    """ansible.pub's new text: the new public half's line, whatever the file held."""

    def text(ctx: Context, held: str) -> str:
        line = ctx.staged(public_name(key))
        if line is None:
            raise StepFailed("no new public half is staged: the plan's ssh.keygen stages it")
        return line + "\n"

    return text


class SshKey:
    name = "ssh-key"
    per_key = False

    def __init__(self, inventory: Path = INVENTORY):
        self.inventory = inventory  # the Ansible inventory whose groups name the hosts

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: ssh-key takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "Ansible SSH key"

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool generates a new SSH key for the ansible user, authorises it on every host "
            f"that holds the key and logs in to each with it, commits its public half to {REPO}, "
            f"{tool_part(leaf)}, switches srviac's copy of the key to it, and takes the old key "
            f"off every host. A VM that may be off is started for it and shut down again.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(f"{leaf.leaf}: an ssh-key plan rotates one key, not {len(leaf.keys)}")
        key = leaf.keys[0]
        try:
            found = hosts(self.inventory)
        except InventoryError as e:
            raise PlanError(f"{leaf.leaf}: {e}") from None
        steps = ctx.steps
        new, old = public_name(key), old_public_name(key)
        add = steps.playbook(
            "add-new-key",
            "authorise the new key for ansible on every host and log in with it",
            book(found, "present", new, value_name(key)),
            book(found, "absent", new),
            vms=found.may_be_off,
        )
        remove = steps.playbook(
            "remove-old-key",
            "take the old key for ansible off every host",
            book(found, "absent", old),
            book(found, "present", old, old_name(key)),
            vms=found.may_be_off,
        )
        commit = steps.commit_file(
            REPO, PUBLIC, committed(key), credentials=CREDENTIALS, message=MESSAGE
        )
        return [
            Reach(steps.ssh, found.always_up),
            *steps.keygen(key, COMMENT),
            *add,
            *commit,
            *steps.write(),
            *steps.key_copy(key),
            *steps.activate(),
            *remove,
        ]
