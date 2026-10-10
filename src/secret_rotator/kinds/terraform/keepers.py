"""The keeper file, config/<stage>/rotation.tfvars: one rotation_epoch map of keeper name to value,
which the hook passes to its Terraform. The kind rewrites it whole in the form `terraform fmt`
leaves unchanged, since every deploy repo's lint runs `terraform fmt -check -recursive`: the keys
quoted and sorted, their `=` aligned, an empty map on one line. Leading comment lines are kept."""

import re

VARIABLE = "rotation_epoch"
FILE = re.compile(
    rf"(?P<head>(?:[ \t]*(?:#[^\n]*)?\n)*){VARIABLE}[ \t]*=[ \t]*\{{(?P<body>[^{{}}]*)\}}\s*"
)
ENTRY = re.compile(
    r'\s*(?:"(?P<quoted>[^"\\]+)"|(?P<bare>[A-Za-z_][A-Za-z0-9_-]*))\s*=\s*"(?P<value>[^"\\]*)"\s*'
)


def parse(text: str) -> tuple[str, dict[str, str]]:
    """The file's leading comment lines and its map; ValueError when it is not one rotation_epoch
    map of quoted values."""
    found = FILE.fullmatch(text)
    if found is None:
        raise ValueError(f"not one `{VARIABLE} = {{ … }}` map")
    epochs: dict[str, str] = {}
    for line in found["body"].splitlines():
        if not line.strip():
            continue
        entry = ENTRY.fullmatch(line)
        if entry is None:
            raise ValueError(f"{line.strip()!r} is not a keeper with a quoted value")
        epochs[entry["quoted"] or entry["bare"]] = entry["value"]
    return found["head"], epochs


def render(head: str, epochs: dict[str, str]) -> str:
    if not epochs:
        return f"{head}{VARIABLE} = {{}}\n"
    width = max(len(name) for name in epochs) + 2
    lines = [f'  {f'"{name}"':<{width}} = "{epochs[name]}"\n' for name in sorted(epochs)]
    return f"{head}{VARIABLE} = {{\n{''.join(lines)}}}\n"
