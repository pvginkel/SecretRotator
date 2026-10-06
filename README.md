# SecretRotator

Automated OpenBao secret rotation for the homelab. The `secret-rotator` command checks every leaf
of OpenBao's `kv` mount against its rotation annotations, rotates the keys that are due, and rolls
each new value out to its consumers.

- The design: AnsibleSpecs `secret-rotation/design.md`, and each leaf's kind, interval and
  activation in `secret-rotation/catalog.md`.
- The operator's side, in Ansible: `docs/runbooks/openbao.md` §5 (rotation and these commands) and
  `docs/runbooks/secret-rotator-go-live.md` (bringing it up).

## Commands

| Command | What it does |
|---|---|
| `secret-rotator audit [--keys FILE]` | Checks every leaf against the annotation contract. Offline with `--keys`: the seed over FILE's key names. |
| `secret-rotator annotate [--apply]` | Writes the seed's annotations by metadata patch and creates the marker leaves it declares. A dry run without `--apply`. |
| `secret-rotator plan <path> [--keys FILE]` | Prints the leaf's plans and executes nothing. Offline with `--keys`. |
| `secret-rotator run <path>` | Runs one plan of the leaf in the terminal, its operator steps as prompts. A plan a failure stopped offers Retry, Abort (roll back) and Details. |
| `secret-rotator run` | The nightly run. |

Exit status: 0 on success, 1 on a finding or a failure, 2 on a usage error. No output carries a
secret value.

The live commands log in to OpenBao at `https://secrets.home:8200` with the `rotator` AppRole, from
`SECRET_ROTATOR_ROLE_ID` and `SECRET_ROTATOR_SECRET_ID`. All but `annotate` also read the prd
cluster with the ServiceAccount token in `SECRET_ROTATOR_K8S_TOKEN`. The AppRole is bound to
srviac's address, so they run on srviac, in the `iac` container:

```sh
ssh -t ansible@srviac "sudo iac -c 'secret-rotator run <leaf>'"
```

## How it ships and runs

- **Build.** `IaC/SecretRotator` runs this repo's `Jenkinsfile` on a push to `main`: lint and tests.
  When both are green it resets the `prd` branch to that commit and starts `IaC/IaC Docker Image`
  with `image=iac`. A red build moves nothing.
- **Install.** The `iac` image (Ansible `support/iac-image/Dockerfile`) installs SecretRotator from
  `prd` as a uv tool with a venv of its own. Every rebuild of the image installs `prd`'s tip, so
  srviac runs the last green commit. `run` names that commit in its first line.
- **Schedule.** `IaC/Scheduled Secret Rotation` (Ansible
  `Jenkinsfile.iac-scheduled-secret-rotation`) runs `secret-rotator run` on srviac at 04:30.
- **Switches.** `src/secret_rotator/switches.yaml` ships in the package: `dry_run`, `paused` (the
  kill switch), `kinds_enabled`, `max_rotations_per_run`, `card_tag` and `telegram_chat_id`. A change
  takes effect once its green build has rebuilt the image. Disabling `IaC/Scheduled Secret Rotation`
  is the immediate stop.
- **Cluster identity.** `k8s/cluster-identity.yaml` holds the `secret-rotator` ServiceAccount on prd,
  bound to `cluster-admin`, and its token Secret. Nothing reconciles it: the operator applies it by
  hand, once, and copies the token into OpenBao `kv/iac/rotator-k8s-token`.

## Layout

- `src/secret_rotator/` is the core: the annotation contract, the audit and the schedule, plans and
  their step factory (`plan`), the executor with its lock and staging leaf, the generic steps
  (`kvsteps`, `k8ssteps`, `jenkinssteps`, `ansiblesteps`, `opsteps`), the nightly run (`nightly`),
  the standing card, and the clients for OpenBao, Kubernetes, Jenkins, YouTrack and Telegram.
- `src/secret_rotator/kinds/<name>/` holds one package per kind: `random`, `manual` and `approle`.
  Each is registered as a `secret_rotator.kinds` entry point in `pyproject.toml`. The leaves of a kind
  the contract knows but no package implements are skipped.
- `src/secret_rotator/seed.yaml` holds the annotations `annotate` writes, transcribed from the
  catalog. `store-keys.json` lists the store's leaves and key names, without values, for the
  offline `--keys` runs and the seed's tests.

## Development

Python 3.13 and poetry live in the KubeCoder `iac` sidecar. `kc project setup`, `lint` and `test`
run them there: `ruff check`, `ruff format --check` and `pytest`. The tests drive fakes of OpenBao,
the cluster, Jenkins, YouTrack, Telegram and `ansible-playbook`, never a live system.
