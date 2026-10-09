"""audit: each departure from design §5 is a finding that names leaf and key, blocks only what it
touches, and no value is read or printed."""

import datetime

from fake_cluster import TOKEN, FakeCluster
from fake_openbao import ROLE_ID, SECRET_ID, FakeOpenBao
from fixtures import (
    COMPLIANT,
    FOREIGN,
    annotated,
    compliant_store,
    data_of,
    edit,
    fields_of,
    found,
    messages,
)

from secret_rotator import cli, registry
from secret_rotator.audit import Leaf, audit, due_keys
from secret_rotator.cluster import Cluster

ENV = {cli.ROLE_ID_ENV: ROLE_ID, cli.SECRET_ID_ENV: SECRET_ID, cli.K8S_TOKEN_ENV: TOKEN}
OIDC = "eso/prd/app/prd/oidc"
TOKEN_LEAF = "eso/prd/app/prd/token"
TRELLO = "eso/prd/trello/prd/trello"
BOT = "eso/prd/bot/prd/config"


class TestContract:
    def test_the_compliant_store_has_no_finding(self):
        assert found(compliant_store()) == []

    def test_keys_the_contract_leaves_to_others_are_ignored(self):
        store = compliant_store()
        for leaf in store.values():
            leaf.meta.update(FOREIGN)
        assert found(store) == []

    def test_a_key_of_the_old_layout_reads_as_a_stale_entry_and_blocks_nothing(self):
        store = compliant_store()
        store["eso/prd/app/prd/token"].meta["rotation_mechanism"] = "random"
        result = audit(store)
        assert messages(store) == [
            "eso/prd/app/prd/token: rotation_mechanism: stale: the leaf has no key 'mechanism'"
        ]
        assert result.blocked_leaves == set() and result.blocked_keys == set()

    def test_a_known_kind_not_implemented_yet_is_no_finding(self):
        store = compliant_store()
        assert fields_of(store["shared/ceph"].meta, "user_key")["kind"] == "cephx"
        assert fields_of(store[OIDC].meta, "client_secret")["kind"] == "keycloak-client"
        assert found(store) == []

    def test_a_key_without_an_entry_or_an_entry_without_kind_or_activate_is_one_finding(self):
        store = compliant_store()
        del store[OIDC].meta["rotation_client_id"]
        assert messages(store) == [f"{OIDC}: rotation_client_id: missing"]
        for field in ("kind", "activate"):
            store = compliant_store()
            edit(store[OIDC].meta, "client_secret", **{field: None})
            assert messages(store) == [f"{OIDC}: rotation_client_secret: {field}: missing"], field
        store = compliant_store()
        edit(store["iac/copy"].meta, "token", activate=None)
        assert messages(store) == ["iac/copy: rotation_token: activate: missing"]

    def test_an_entry_without_an_interval_takes_the_14_day_default(self):
        store = compliant_store()
        edit(store["eso/prd/bot/prd/config"].meta, "jenkins-token", interval=None)
        assert found(store) == []
        assert audit(store).entries["eso/prd/bot/prd/config"]["jenkins-token"].interval == 14

    def test_an_entry_that_is_no_json_object_is_a_finding(self):
        for value, message in (("random", "not JSON"), ('["random"]', "not a JSON object")):
            store = compliant_store()
            store[TOKEN_LEAF].meta["rotation_token"] = value
            assert messages(store) == [f"{TOKEN_LEAF}: rotation_token: {message}"], value

    def test_a_field_the_entry_does_not_have(self):
        store = compliant_store()
        edit(store[TOKEN_LEAF].meta, "token", mechanism="random")
        assert messages(store) == [
            f"{TOKEN_LEAF}: rotation_token: mechanism: not a field of the entry"
        ]

    def test_an_unknown_kind(self):
        store = compliant_store()
        edit(store[OIDC].meta, "client_secret", kind="keycloak")
        edit(store["eso/prd/bot/prd/config"].meta, "telegram-bot-token", kind="telegram")
        assert found(store) == [
            (OIDC, "rotation_client_secret"),
            ("eso/prd/bot/prd/config", "rotation_telegram-bot-token"),
        ]
        assert "kind: unknown kind 'keycloak': not a kind of design §6" in messages(store)[0]

    def test_a_kind_that_may_not_rotate_the_key(self):
        store = compliant_store()
        edit(store["eso/prd/es/prd/creds"].meta, "username", kind="elastic-user", activate="auto")
        assert messages(store) == [
            "eso/prd/es/prd/creds: rotation_username: kind: elastic-user does not rotate username"
        ]

    def test_a_kind_that_rotates_one_key_of_a_seed_s_leaf_may_be_named_by_several(self):
        store = compliant_store()
        edit(store["shared/ceph"].meta, "user_id", kind="cephx", interval="365d", activate="none")
        assert found(store) == []

    def test_a_stale_entry_blocks_nothing(self):
        store = compliant_store()
        edit(store["shared/wifi"].meta, "public", kind="none")
        result = audit(store)
        assert messages(store) == [
            "shared/wifi: rotation_public: stale: the leaf has no key 'public'"
        ]
        assert result.blocked_leaves == set() and result.blocked_keys == set()

    def test_a_copy_whose_primary_leaf_or_key_is_missing(self):
        store = compliant_store()
        catalog = store["eso/prd/kc/prd/catalog"].meta
        edit(catalog, "client-id", kind="copy:eso/prd/gone/prd/oidc#client_id")
        edit(catalog, "client-secret", kind=f"copy:{OIDC}#secret")
        edit(store["iac/copy"].meta, "token", kind=f"copy:{TOKEN_LEAF}#gone")
        assert messages(store) == [
            "eso/prd/kc/prd/catalog: rotation_client-id: copy of eso/prd/gone/prd/oidc#client_id: "
            "no leaf eso/prd/gone/prd/oidc",
            f"eso/prd/kc/prd/catalog: rotation_client-secret: copy of {OIDC}#secret: {OIDC} has no "
            "key 'secret'",
            f"iac/copy: rotation_token: copy of {TOKEN_LEAF}#gone: {TOKEN_LEAF} has no key 'gone'",
        ]

    def test_a_copy_of_a_none_key_is_valid(self):
        store = compliant_store()
        catalog = store["eso/prd/kc/prd/catalog"].meta
        assert fields_of(catalog, "client-id")["kind"] == f"copy:{OIDC}#client_id"
        assert found(store) == []

    def test_a_copy_of_a_copy_is_a_finding(self):
        store = compliant_store()
        edit(store["shared/wifi"].meta, "password", kind="copy:iac/copy#token", interval=None)
        edit(store["shared/wifi"].meta, "password", notes=None)
        assert messages(store) == [
            "shared/wifi: rotation_password: copy of iac/copy#token, which is itself a copy"
        ]

    def test_a_malformed_interval_or_never_without_the_key_s_own_notes(self):
        for value in ("14", "2w", "0d", "14 d", "Never"):
            store = compliant_store()
            edit(store[TOKEN_LEAF].meta, "token", interval=value)
            assert found(store) == [(TOKEN_LEAF, "rotation_token")], value
        store = compliant_store()
        edit(store[TOKEN_LEAF].meta, "token", interval="never")
        assert messages(store) == [f"{TOKEN_LEAF}: rotation_token: interval: never without notes"]
        edit(store[TOKEN_LEAF].meta, "token", notes="  ")
        assert found(store) == [(TOKEN_LEAF, "rotation_token")]
        edit(store[TOKEN_LEAF].meta, "token", notes="why it never rotates")
        assert found(store) == []

    def test_a_never_key_needs_its_own_notes_not_another_key_s(self):
        store = compliant_store()
        edit(store[TRELLO].meta, "bearer-token", interval="never")
        assert messages(store) == [
            f"{TRELLO}: rotation_bearer-token: interval: never without notes"
        ]

    def test_a_field_a_copy_or_a_none_key_does_not_take(self):
        store = compliant_store()
        edit(store["eso/prd/kc/prd/catalog"].meta, "client-secret", interval="14d")
        edit(store["eso/prd/bot/prd/config"].meta, "telegram-chat-id", activate="auto")
        edit(store[OIDC].meta, "client_id", args={"realm": "homelab"})
        assert messages(store) == [
            f"{OIDC}: rotation_client_id: args: a none key takes none",
            "eso/prd/bot/prd/config: rotation_telegram-chat-id: activate: a none key takes none",
            "eso/prd/kc/prd/catalog: rotation_client-secret: interval: a copy takes none",
        ]

    def test_the_activate_forms(self):
        valid = [
            "auto",
            "none",
            "eso",
            "k8s-rollout",
            "eso,k8s-rollout,manual:say so",
            "k8s-rollout:ns/deployment/a",
            "k8s-rollout:ns/deployment/a,ns/daemonset/b",
            "jenkins-credential:724520d1",
            "jenkins-job:IaC/Apply",
            "jenkins-job:YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true&X=1",
            "github-webhook:pvginkel/Fieldnotes/123",
            "argocd-sync:iot-prd",
            "manual:re-encrypt ca.json then roll step-ca",
        ]
        invalid = [
            "restart",
            "auto,manual:say so",
            "none,eso",
            "eso:x",
            "k8s-rollout:a/b",
            "k8s-rollout,ns/deployment/a",
            "eso,ns/deployment/a",
            "k8s-rollout:ns/pod/a",
            "jenkins-credential:",
            "github-webhook:repo",
            "github-webhook:Fieldnotes/123",
            "argocd-sync:",
            "manual:",
            "manual: ",
            "",
            "eso, manual:x",
        ]
        for value in valid:
            store = compliant_store()
            edit(store[TOKEN_LEAF].meta, "token", activate=value)
            assert found(store) == [], value
        for value in invalid:
            store = compliant_store()
            edit(store[TOKEN_LEAF].meta, "token", activate=value)
            assert (TOKEN_LEAF, "rotation_token") in found(store), value
            assert messages(store)[0].startswith(f"{TOKEN_LEAF}: rotation_token: activate: ")

    def test_args_that_are_not_a_json_object(self):
        for value in ('{"realm":"homelab"}', ["homelab"], 3):
            store = compliant_store()
            edit(store[OIDC].meta, "client_secret", args=value)
            assert messages(store) == [
                f"{OIDC}: rotation_client_secret: args: not a JSON object"
            ], value

    def test_with_the_plugins_args_its_kind_cannot_use_are_a_finding_that_blocks_the_key(self):
        store = compliant_store()
        edit(store[BOT].meta, "telegram-bot-token", args={"type": "fax"})
        edit(store[TOKEN_LEAF].meta, "token", args={"length": 0})
        result = audit(store, plugins=registry.load())
        assert [str(f) for f in result.findings] == [
            f"{TOKEN_LEAF}: rotation_token: args: length: not a whole number from 1",
            f"{BOT}: rotation_telegram-bot-token: args: type: 'fax' is not a credential type "
            "manual documents",
        ]
        assert result.blocked(BOT, "telegram-bot-token") and result.blocked(TOKEN_LEAF, "token")
        assert not result.blocked(BOT, "jenkins-token")

    def test_with_the_plugins_args_that_are_no_json_object_are_a_finding_not_a_crash(self):
        store = compliant_store()
        edit(store[TOKEN_LEAF].meta, "token", args="x")
        result = audit(store, plugins=registry.load())
        assert [str(f) for f in result.findings] == [
            f"{TOKEN_LEAF}: rotation_token: args: not a JSON object"
        ]

    def test_with_the_plugins_a_type_manual_documents_is_no_finding(self):
        store = compliant_store()
        edit(store[BOT].meta, "telegram-bot-token", args={"type": "telegram-bot-token"})
        assert audit(store, plugins=registry.load()).findings == []

    def test_an_expiry_that_is_not_an_iso_date(self):
        for value in ("2027-13-01", "20270131", "soon", "2027-01-31T00:00:00", 20270131):
            store = compliant_store()
            edit(store["jenkins/youtrack"].meta, "admin-token", expires_at=value)
            assert found(store) == [("jenkins/youtrack", "rotation_admin-token")], value

    def test_a_leaf_whose_keys_cannot_be_read_is_a_finding_and_its_entries_still_checked(self):
        store = compliant_store()
        store["shared/wifi"].keys = None
        edit(store["shared/wifi"].meta, "password", interval="2w")
        assert found(store) == [("shared/wifi", "(data)"), ("shared/wifi", "rotation_password")]

    def test_an_unannotated_leaf_is_one_finding_per_key(self):
        store = compliant_store()
        store["eso/prd/bot/prd/config"].meta = {"rotation": "coordinated"}
        assert found(store) == [
            ("eso/prd/bot/prd/config", "rotation_jenkins-token"),
            ("eso/prd/bot/prd/config", "rotation_telegram-bot-token"),
            ("eso/prd/bot/prd/config", "rotation_telegram-chat-id"),
        ]


class TestScope:
    """A finding on a key's entry blocks the key and the primary it copies; a leaf-level one the
    leaf and the primaries copied in; a stale entry nothing."""

    def test_a_compliant_store_blocks_nothing(self):
        result = audit(compliant_store())
        assert result.blocked_leaves == set()
        assert result.blocked_keys == set()

    def test_a_finding_on_a_key_s_entry_blocks_that_key_alone(self):
        store = compliant_store()
        edit(store[TRELLO].meta, "bearer-token", interval="3w")
        store[OIDC].keys.add("url")
        result = audit(store)
        assert result.blocked_leaves == set()
        assert result.blocked_keys == {(TRELLO, "bearer-token"), (OIDC, "url")}
        assert not result.blocked(TRELLO, "api-key")
        assert not result.blocked(OIDC, "client_secret")
        assert "bearer-token" not in result.entries[TRELLO]

    def test_a_finding_on_a_copy_s_entry_blocks_the_copy_and_its_primary_key(self):
        store = compliant_store()
        edit(store["eso/prd/kc/prd/catalog"].meta, "client-secret", activate="restart")
        result = audit(store)
        assert result.blocked_leaves == set()
        assert result.blocked_keys == {
            ("eso/prd/kc/prd/catalog", "client-secret"),
            (OIDC, "client_secret"),
        }
        assert not result.blocked(OIDC, "client_id")

    def test_a_leaf_level_finding_blocks_the_leaf_and_every_primary_copied_into_it(self):
        store = compliant_store()
        store["eso/prd/kc/prd/catalog"].keys = None
        result = audit(store)
        assert result.blocked_leaves == {"eso/prd/kc/prd/catalog"}
        assert result.blocked_keys == {(OIDC, "client_id"), (OIDC, "client_secret")}
        assert result.blocked("eso/prd/kc/prd/catalog", "jenkins-user")
        assert not result.blocked(TOKEN_LEAF, "token")

    def test_a_blocked_copy_blocks_that_primary_key(self):
        store = compliant_store()
        edit(store["iac/copy"].meta, "token", activate=None)
        result = audit(store)
        assert result.blocked_keys == {("iac/copy", "token"), (TOKEN_LEAF, "token")}

    def test_a_leaf_level_finding_on_a_primary_leaves_its_copies_leaves_unblocked(self):
        store = compliant_store()
        store[TOKEN_LEAF].keys = None
        result = audit(store)
        assert result.blocked_leaves == {TOKEN_LEAF}
        assert not result.blocked("iac/copy", "token")


class TestNeverAndDue:
    def test_keys_that_never_rotate_are_listed(self):
        assert audit(compliant_store()).never == [
            (TRELLO, "api-key"),
            (TRELLO, "token"),
            ("shared/wifi", "password"),
        ]

    def test_with_no_stamp_every_unblocked_scheduled_key_is_due_oldest_first(self):
        store = compliant_store()
        store[TOKEN_LEAF].state.stamps["token"] = "2026-10-01"
        edit(store["shared/ceph"].meta, "user_key", interval="2w")
        edit(store["shared/wifi"].meta, "password", expires_at="soon")
        edit(store[TRELLO].meta, "bearer-token", interval="3w")
        today = datetime.date(2026, 10, 20)
        due = due_keys(store, audit(store), today)
        assert [(s.leaf, s.key) for s in due] == [
            (OIDC, "client_secret"),
            ("eso/prd/bot/prd/config", "jenkins-token"),
            ("eso/prd/bot/prd/config", "telegram-bot-token"),
            ("eso/prd/es/prd/creds", "password"),
            ("eso/prd/yt/prd/webhook", "token"),
            ("jenkins/youtrack", "admin-token"),
            (TOKEN_LEAF, "token"),
        ]

    def test_a_stamp_on_the_leaf_s_metadata_is_not_read(self):
        store = compliant_store()
        store[TOKEN_LEAF].meta["rotated_at_token"] = "2026-10-01"
        due = due_keys(store, audit(store), datetime.date(2026, 10, 2))
        assert [s.due_at for s in due if s.leaf == TOKEN_LEAF] == [datetime.date.min]

    def test_a_key_s_entry_expiry_brings_it_due_from_the_lead_before_it(self):
        store = compliant_store()
        store["jenkins/youtrack"].state.stamps["admin-token"] = "2027-01-20"
        due = due_keys(store, audit(store), datetime.date(2027, 1, 24))
        assert [s.due_at for s in due if s.leaf == "jenkins/youtrack"] == [
            datetime.date(2027, 1, 24)
        ]


class Run:
    """The command line against a fake OpenBao and a fake cluster."""

    def __init__(self, bao, env=ENV, cluster=None):
        self.bao, self.env = bao, env
        self.cluster = cluster or FakeCluster()

    def __call__(self, *argv):
        self.lines = []
        code = cli.main(
            list(argv),
            opener=self.bao,
            out=self.lines.append,
            environ=self.env,
            kube=self.cluster.kube,
        )
        self.text = "\n".join(self.lines)
        return code


def annotated_bao():
    return FakeOpenBao(
        {
            path: {"data": data_of(path), "meta": {**meta, **FOREIGN}}
            for path, (_, meta) in COMPLIANT.items()
        }
    )


class TestLiveAudit:
    def test_a_compliant_store_exits_0(self):
        run = Run(annotated_bao())
        assert run("audit") == 0, run.text
        assert run.lines[-1] == (
            f"0 finding(s) on 0 of {len(COMPLIANT)} leaf(s); blocked: 0 "
            f"leaf(s), 0 key(s); 3 key(s) never rotate"
        )
        assert "never: shared/wifi#password" in run.lines

    def test_it_logs_in_with_the_rotators_approle(self):
        bao = annotated_bao()
        Run(bao)("audit")
        assert bao.requests[0][:2] == ("POST", "auth/approle/login")

    def test_findings_exit_1_and_print_no_value(self):
        bao = annotated_bao()
        edit(bao.leaves["shared/wifi"]["meta"], "password", kind="wifi")
        run = Run(bao)
        assert run("audit") == 1
        assert (
            "shared/wifi: rotation_password: kind: unknown kind 'wifi': not a kind of design §6, "
            "none, or copy:<path>#<key>" in run.lines
        )
        assert "SECRET" not in run.text

    def test_the_plugins_check_each_keys_args(self):
        bao = annotated_bao()
        edit(bao.leaves["shared/wifi"]["meta"], "password", args={"type": "fax"})
        run = Run(bao)
        assert run("audit") == 1
        assert (
            "shared/wifi: rotation_password: args: type: 'fax' is not a credential type manual "
            "documents"
        ) in run.lines

    def test_it_reads_key_names_never_values_and_writes_nothing(self):
        bao = annotated_bao()
        Run(bao)("audit")
        assert bao.writes() == []
        assert {(m, p.split("/")[1]) for m, p, *_ in bao.requests[1:]} == {
            ("LIST", "metadata"),
            ("GET", "metadata"),
            ("GET", "subkeys"),
        }

    def test_a_deleted_current_version_is_a_finding(self):
        bao = annotated_bao()
        bao.leaves["shared/wifi"]["data"] = None
        run = Run(bao)
        assert run("audit") == 1
        assert run.lines[0] == (
            "shared/wifi: (data): its current version is deleted or "
            "destroyed: its keys cannot be read"
        )

    def test_the_rotators_working_leaves_are_not_checked_or_read(self):
        bao = annotated_bao()
        bao.leaves["rotator/lock"] = {"data": {"holder": ""}, "meta": {}}
        bao.leaves["rotator/state"] = {"data": {"eso/prd/app/prd/token": "{}"}, "meta": {}}
        bao.leaves["rotator/staging/random/eso/prd/app/prd/token"] = {
            "data": {"token": "SECRET-staged"},
            "meta": {},
        }
        run = Run(bao)
        assert run("audit") == 0, run.text
        working = ("rotator/lock", "rotator/state", "staging")
        assert not [p for _, p, *_ in bao.requests if any(w in p for w in working)]


class TestOrphans:
    """The orphan check (design §3.3): an eso/prd/ leaf nothing references, following copies
    (045's RV2), by the one match of ruling B1."""

    def referenced(self, fake=None):
        return Cluster((fake or FakeCluster()).kube()).referenced()

    def orphans(self, store, fake=None):
        result = audit(store, self.referenced(fake))
        return [str(f) for f in result.findings], result

    def without(self, *names):
        fake = FakeCluster()
        for ns, name in names:
            del fake.objects["externalsecrets", ns, name]
        return fake

    def test_the_compliant_store_on_the_compliant_cluster_has_none(self):
        assert self.orphans(compliant_store())[0] == []

    def test_an_eso_prd_leaf_nothing_references_is_an_orphan_and_blocked(self):
        findings, result = self.orphans(compliant_store(), self.without(("trello-prd", "trello")))
        assert findings == [
            "eso/prd/trello/prd/trello: (consumers): an orphan: no ExternalSecret references it "
            "or a leaf it is copied into"
        ]
        assert "eso/prd/trello/prd/trello" in result.blocked_leaves

    def test_the_catalog_extracted_by_data_from_alone_is_no_orphan(self):
        assert "eso/prd/kc/prd/catalog" in self.referenced()

    def test_a_primary_counts_as_consumed_through_a_consumed_copy(self):
        # eso/prd/app/prd/oidc's keys are copied into the catalog, which is referenced.
        fake = self.without(("app-prd", "app-oidc"))
        assert self.orphans(compliant_store(), fake)[0] == []

    def test_an_orphaned_copy_leaf_orphans_its_primary_and_blocks_what_is_copied_into_it(self):
        fake = self.without(("app-prd", "app-oidc"), ("kubecoder-prd", "kubecoder-secret-catalog"))
        findings, result = self.orphans(compliant_store(), fake)
        assert [f.split(":")[0] for f in findings] == [
            "eso/prd/app/prd/oidc",
            "eso/prd/kc/prd/catalog",
        ]
        assert result.blocked("eso/prd/app/prd/oidc", "client_secret")

    def test_a_copy_outside_eso_prd_counts_as_consumed(self):
        # eso/prd/yt/prd/webhook: no ExternalSecret; its copy is in jenkins/youtrack.
        assert "eso/prd/yt/prd/webhook" not in self.referenced()
        store = compliant_store()
        assert self.orphans(store)[0] == []
        del store["jenkins/youtrack"]
        assert self.orphans(store)[0] == [
            "eso/prd/yt/prd/webhook: (consumers): an orphan: no ExternalSecret references it or "
            "a leaf it is copied into"
        ]

    def test_eso_dev_and_other_leaves_are_not_checked(self):
        store = compliant_store()
        store["eso/dev/app/dev/token"] = Leaf(
            "eso/dev/app/dev/token",
            {"token"},
            annotated({"token": {"kind": "random", "interval": "14d", "activate": "none"}}),
        )
        findings, _ = self.orphans(store, FakeCluster([]))
        # Nothing on the cluster: every eso/prd/ leaf is an orphan but the two whose copy is
        # outside eso/prd/ (eso/prd/app/prd/token in iac/copy, eso/prd/yt/prd/webhook in
        # jenkins/youtrack); eso/dev/ and the rest of the mount are not checked.
        assert [f.split(":")[0] for f in findings] == [
            "eso/prd/app/prd/oidc",
            "eso/prd/bot/prd/config",
            "eso/prd/es/prd/creds",
            "eso/prd/kc/prd/catalog",
            "eso/prd/trello/prd/trello",
        ]

    def test_without_the_cluster_there_is_no_orphan_check(self):
        store = compliant_store()
        store["eso/prd/nothing/reads/it"] = Leaf(
            "eso/prd/nothing/reads/it",
            {"token"},
            annotated({"token": {"kind": "random", "interval": "14d", "activate": "auto"}}),
        )
        assert audit(store).findings == []

    def test_the_live_audit_reports_an_orphan(self):
        run = Run(annotated_bao(), cluster=self.without(("trello-prd", "trello")))
        assert run("audit") == 1
        assert run.lines[0].startswith("eso/prd/trello/prd/trello: (consumers): an orphan")
