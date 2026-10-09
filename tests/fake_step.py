"""A stand-in for step-cli's `step crypto jwe decrypt --password-file <file>`, run as
`python fake_step.py <dir> crypto jwe decrypt --password-file <file>`: it appends how it was called
and the password it read to calls.jsonl in <dir>, then opens the fakes' JWE on its stdin
(fake_step_ca.sealed) as step-cli opens a JWE: the private JWK on stdout, exit 0; a password that
does not open it exits 1 with step-cli 0.31's message."""

import base64
import json
import sys
from pathlib import Path

PREFIX = "FAKE-JWE."
REFUSED = "error decrypting data: go-jose/go-jose: error in cryptographic primitive"


def main():
    where, *argv = sys.argv[1:]
    where = Path(where)
    assert argv[:4] == ["crypto", "jwe", "decrypt", "--password-file"], argv
    password = Path(argv[4]).read_text()
    jwe = sys.stdin.read()
    with (where / "calls.jsonl").open("a") as f:
        f.write(json.dumps({"argv": argv, "password": password}) + "\n")
    try:
        doc = json.loads(base64.urlsafe_b64decode(jwe.removeprefix(PREFIX)))
    except ValueError:
        print(
            "error parsing data: go-jose/go-jose: compact JWE format must have five parts",
            file=sys.stderr,
        )
        sys.exit(1)
    if doc["password"] != password:
        print(REFUSED, file=sys.stderr)
        sys.exit(1)
    print(json.dumps(doc["jwk"], indent=2))


if __name__ == "__main__":
    main()
