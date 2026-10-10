# SecretRotator

Automated OpenBao secret rotation for the homelab. The `secret-rotator` command checks every leaf
of OpenBao's `kv` mount against its rotation annotations, rotates the keys that are due, and rolls
each new value out to its consumers. A data key's annotations are one custom-metadata entry,
`rotation_<key>`, a JSON object of its kind, interval, args, activation, `expires_at` and notes.
The rotator's run state — each key's rotation stamp, each leaf's status — is its own leaf,
`kv/rotator/state`, never a secret leaf's metadata.

- The design: AnsibleSpecs `secret-rotation/design.md`, and each leaf's kind, interval and
  activation in `secret-rotation/catalog.md`.
- The operator's side, in Ansible: `docs/runbooks/openbao.md` §5 (rotation and these commands) and
  `docs/runbooks/secret-rotator-go-live.md` (bringing it up) and `docs/runbooks/external-key-due.md`
  (an `external` key that fell due).

## Commands

| Command | What it does |
|---|---|
| `secret-rotator audit [--keys FILE]` | Checks every leaf against the annotation contract. Offline with `--keys`: the seed over FILE's key names. |
| `secret-rotator annotate [--apply]` | Makes each seed leaf's custom metadata exactly its `rotation_<key>` entries by metadata patch: it adds or changes the entries, removes every other key, sets an automatic leaf's `max_versions` to 20, and creates the marker leaves the seed declares. A dry run without `--apply` lists every write. |
| `secret-rotator plan <path> [--keys FILE [--snapshot FILE]]` | Prints the leaf's plans and executes nothing. Offline with `--keys`; with `--snapshot`, the plan takes the ExternalSecrets it syncs and the workloads it rolls out from FILE, a read-only snapshot of the prd cluster's ExternalSecrets, Deployments, StatefulSets and DaemonSets (`plan --help` prints the `kubectl get` that takes it). Without it, an offline plan that reads the cluster cannot be built: one of a leaf activated through the cluster, or of a `terraform` marker. |
| `secret-rotator run <path>` | Runs one plan of the leaf in the terminal, its operator steps as prompts. A plan a failure stopped offers Retry, Abort (roll back) and Details. Then pushes the run state's metrics. |
| `secret-rotator ui` | The operator's terminal UI. It lists every plan with an operator step as a box, due or not: the plans in flight and failed first, then by when they fall due, the earliest first, then those of keys without a due date. It runs one plan at a time as a wizard: each operator step is a screen, and the tool steps between them show their progress. An `external` key's box shows the key's notes and Done, which stamps it. `f` narrows the list to the selected box's type, a `manual` key's credential type or else its kind. |
| `secret-rotator run` | The nightly run. It ends by pushing its metrics. |
| `secret-rotator stamp <path> <key> [--rotated-at DATE] [--expires-at DATE \| --clear-expires-at]` | Sets the key's rotation stamp in the run state to the date its current value was written, for a value written outside a rotation; sets or clears the `expires_at` in its entry. Takes at least one option. Then pushes the run state's metrics. |

Exit status: 0 on success, 1 on a finding or a failure, 2 on a usage error. No output carries a
secret value.

The live commands log in to OpenBao at `https://secrets.home:8200` with the `rotator` AppRole, from
`SECRET_ROTATOR_ROLE_ID` and `SECRET_ROTATOR_SECRET_ID`. All but `annotate` and `stamp` also read
the prd cluster with the ServiceAccount token in `SECRET_ROTATOR_K8S_TOKEN`; a plan that rotates
that token switches the running command to the new one before it deletes the old. `run` and `stamp`
push their metrics with that token; `stamp` without it stamps all the same. The rotator has no
identity on the dev cluster: the `k8s-sa-token` kind alone reaches it, with the dev write token the
KubeCoder catalog holds. The AppRole is bound to srviac's address, so they run on srviac, in the
`iac` container:

```sh
ssh -t ansible@srviac "sudo iac -c 'secret-rotator run <leaf>'"
```

The UI starts with `ssh -t ansible@srviac secret-rotator-ui` (Ansible `support/iac-agent/bin/`),
which runs it in srviac's tmux session `secret-rotator`: a lost SSH session leaves the UI running
there, and the same command reattaches to it.

## How it ships and runs

- **Build.** `IaC/SecretRotator` runs this repo's `Jenkinsfile` on a push to `main`: lint and tests.
  When both are green it resets the `prd` branch to that commit and starts `IaC/IaC Docker Image`
  with `image=iac`. Its last stage publishes `dashboards/` into the Grafana folder "Secret
  rotation" with JenkinsPipelineUtils' `grafanaDashboards.publish` and Jenkins' Grafana token,
  `kv/jenkins/grafana-api`; until the operator has stored that token, the stage fails. A build red
  at its lint or tests moves nothing; one red at the publish has already moved `prd`.
- **Install.** The `iac` image (Ansible `support/iac-image/Dockerfile`) installs SecretRotator from
  `prd` as a uv tool with a venv of its own. Every rebuild of the image installs `prd`'s tip, so
  srviac runs the last commit whose lint and tests were green. `run` names that commit in its first line.
- **Schedule.** `IaC/Scheduled Secret Rotation` (Ansible
  `Jenkinsfile.iac-scheduled-secret-rotation`) runs `secret-rotator run` on srviac at 05:30.
- **Switches.** `src/secret_rotator/switches.yaml` ships in the package: `dry_run`, `paused` (the
  kill switch), `kinds_enabled`, `max_rotations_per_run`, `card_tag` and `telegram_chat_id`. A change
  takes effect once its build has reset `prd` and the image is rebuilt. Disabling `IaC/Scheduled Secret Rotation`
  is the immediate stop. They govern the nightly run: `run <path>` and `ui` read `telegram_chat_id`
  alone.
- **Cluster identity.** `k8s/cluster-identity.yaml` holds the `secret-rotator` ServiceAccount on prd,
  bound to `cluster-admin`, and its token Secret, named by `generateName`. Nothing reconciles it: the
  operator creates it by hand, once, with `kubectl create -f` (`apply` refuses a `generateName`), and
  copies the token into OpenBao `kv/iac/rotator-k8s-token`. The `k8s-sa-token` kind replaces the
  token yearly: it creates a token Secret, writes its token to the leaf, then deletes the Secret it
  replaced. An `iac` container takes the leaf's token when it starts.

## Metrics and the dashboard

At its end, the nightly run, `run <path>`, `stamp` and `ui` each PUT the group `state` to the
Pushgateway in `prometheus-prd`, through the Kubernetes API's service
proxy with the `SECRET_ROTATOR_K8S_TOKEN` token: each key's due day and rotation stamp, and each
leaf's status. The nightly run adds `audit`, the findings, and `nightly`, its run health; a night
that found the lock held or that `paused` stopped pushes `nightly` alone. A group not pushed is one
line, `metrics: the <group> group is not pushed: <error>`, and changes nothing else. The series are
AnsibleSpecs design §3.4's. PrometheusDeploy's rule group `secret-rotator` alerts on them:
`SecretRotatorStale`, `SecretRotationFailed` and `SecretRotationOverdue`.

`dashboards/secret-rotation.json` is the Grafana dashboard "Secret rotation" (uid
`secret-rotation`), kept by hand, which the build publishes. A change made in Grafana's UI is lost
at the next publish unless it is exported and committed here. The dashboard takes its Prometheus
datasource from its variable `datasource`, and `tests/test_dashboard.py` holds its queries to the
series `metrics.HELP` names.

## Layout

- `src/secret_rotator/` is the core: the annotation contract, the audit and the schedule, plans and
  their step factory (`plan`), the executor with its lock, the staging leaf that records a plan in
  flight (`staging`) and the run state (`state`), the run handling `run <path>` and the UI share
  (`session`), the list the UI shows (`listing`), the generic steps (`kvsteps`, `k8ssteps`,
  `argocdsteps`, `jenkinssteps`, `githubsteps`, `ansiblesteps`, `sshsteps`, `opsteps`), the
  nightly run (`nightly`), the standing card, the metrics (`metrics`), and the clients for OpenBao,
  Kubernetes, Jenkins, GitHub, YouTrack and Telegram.
- `src/secret_rotator/ui/` is `secret-rotator ui`, a Textual app over the executor: the app
  (`app.py`) and its stylesheet (`app.tcss`), the widgets, the wizard's screens collated from a
  plan's steps (`collate`), and `run.py`, which runs a plan's executor on a thread of its own.
- `src/secret_rotator/kinds/<name>/` holds one package per kind: `random`, `manual`, `approle`,
  `keycloak_client`, `cnpg_role`, `jenkins_token`, `jenkins_job_token`, `grafana_admin`,
  `external`, `pve_root_password`, `samba_user`, `step_ca_password`, `github_webhook_secret`,
  `youtrack_token`, `home_assistant_token`, `google_sa_key`, `elastic_user`, `kubecoder_client`,
  `k8s_sa_token` and `terraform`. Each is registered as a `secret_rotator.kinds` entry point in
  `pyproject.toml`, under its kind's name (`keycloak-client` for `keycloak_client`). The leaves of
  a kind the contract knows but no package implements are skipped. A kind that reaches a system of
  its own keeps that system's address in its package: Keycloak's two realms (`keycloak_client`, the
  one place a realm's URL is set), Postgres (`cnpg_role`), Grafana (`grafana_admin`), the PVE nodes
  (`pve_root_password`), step-ca (`step_ca_password`), Home Assistant (`home_assistant_token`),
  Google's token endpoint and IAM API (`google_sa_key`), Elasticsearch (`elastic_user`) and
  KubeCoder's controller (`kubecoder_client`). `youtrack_token` reaches YouTrack and its Hub through
  the core's YouTrack client, `github_webhook_secret` GitHub through the core's `github.webhook`
  step, and `k8s_sa_token` the dev cluster at the apiserver and CA the KubeCoder catalog's
  `kubeconfig-dev-write` names. `terraform` reaches GitHub through the core's GitHub client, under
  its own token `rotator/terraform/credentials`, and Argo CD through the core's `argocd.sync` step.
  `kinds/manual/types/<type>.md` holds one
  document per credential type: the standard instructions a `manual` key's `type` arg picks, under
  a front matter of the credential's name, the shape a pasted value has (and whether it is entered
  as lines) and whether it expires.
- `src/secret_rotator/seed.yaml` holds the annotations `annotate` writes, transcribed from the
  catalog, in a compact form: per leaf, a default and per-key fields under `keys:`, which `annotate`
  expands into one entry per data key. `store-keys.json` lists the store's leaves and key names,
  without values, for the offline `--keys` runs and the seed's tests.
- `dashboards/` holds the Grafana dashboards the build publishes, one `*.json` file each.

## Development

Python 3.13 and poetry live in the KubeCoder `iac` sidecar. `kc project setup`, `lint` and `test`
run them there: `ruff check`, `ruff format --check` and `pytest`. The tests drive fakes of OpenBao,
the cluster with Argo CD's Applications and the Pushgateway behind it, CloudNativePG with its
Postgres login, Keycloak, Jenkins, Grafana, YouTrack and its Hub, Telegram, step-ca, GitHub, Home
Assistant, Google, Elasticsearch, KubeCoder's controller, `ansible-playbook`, `ssh` and step-cli,
never a live system. The UI's pilot tests (`test_ui`, `test_wizard`, `test_wizard_failure`,
`test_filter`) drive the Textual app over the executor and the fake OpenBao, with `tests/sim.py`'s
steps standing in for a vendor's side.
