"""audit: each departure from design §5 is a finding that names leaf and key, blocks only what it
touches, and no value is read or printed."""

import datetime
import json

from fake_cluster import TOKEN, FakeCluster
from fake_openbao import ROLE_ID, SECRET_ID, FakeOpenBao
from fixtures import COMPLIANT, FOREIGN, compliant_store, data_of, found, messages

from secret_rotator import cli
from secret_rotator.audit import Leaf, audit, due_keys
from secret_rotator.cluster import Cluster

ENV = {cli.ROLE_ID_ENV: ROLE_ID, cli.SECRET_ID_ENV: SECRET_ID, cli.K8S_TOKEN_ENV: TOKEN}


class TestContract:
    def test_the_compliant_store_has_no_finding(self):
        assert found(compliant_store()) == []

    def test_keys_the_contract_leaves_to_others_are_ignored(self):
        store = compliant_store()
        for leaf in store.values():
            leaf.meta.update(FOREIGN)
        assert found(store) == []

    def test_a_known_kind_not_implemented_yet_is_no_finding(self):
        store = compliant_store()
        assert store["shared/ceph"].meta["rotation_mechanism"] == "cephx"
        assert store["eso/prd/app/prd/oidc"].meta["rotation_mechanism"] == "keycloak-client"
        assert found(store) == []

    def test_a_missing_mechanism_or_activate_is_one_finding_each(self):
        for key in ("rotation_mechanism", "rotation_activate"):
            store = compliant_store()
            del store["eso/prd/app/prd/oidc"].meta[key]
            assert found(store) == [("eso/prd/app/prd/oidc", key)], key

    def test_a_missing_interval_is_a_finding_where_a_key_is_neither_copy_nor_none(self):
        store = compliant_store()
        del store["eso/prd/bot/prd/config"].meta["rotation_interval"]
        assert found(store) == [("eso/prd/bot/prd/config", "rotation_interval")]

    def test_a_leaf_of_copies_and_none_keys_needs_no_interval(self):
        store = compliant_store()
        assert "rotation_interval" not in store["eso/prd/kc/prd/catalog"].meta
        assert "rotation_interval" not in store["iac/copy"].meta
        assert found(store) == []

    def test_an_unknown_kind_in_the_mechanism_or_an_override(self):
        store = compliant_store()
        store["eso/prd/app/prd/oidc"].meta["rotation_mechanism"] = "keycloak"
        store["eso/prd/bot/prd/config"].meta["key_telegram-bot-token"] = "telegram"
        assert found(store) == [
            ("eso/prd/app/prd/oidc", "rotation_mechanism"),
            ("eso/prd/bot/prd/config", "key_telegram-bot-token"),
        ]
        assert "unknown kind 'keycloak': not a kind of design §6" in messages(store)[0]

    def test_a_key_its_kind_does_not_own_and_no_override_names(self):
        store = compliant_store()
        store["eso/prd/app/prd/oidc"].keys.add("url")
        del store["eso/prd/es/prd/creds"].meta["key_username"]
        assert found(store) == [
            ("eso/prd/app/prd/oidc", "url"),
            ("eso/prd/es/prd/creds", "username"),
        ]

    def test_a_one_key_kind_owns_no_key_when_two_are_left(self):
        store = compliant_store()
        del store["eso/prd/bot/prd/config"].meta["key_telegram-chat-id"]
        assert found(store) == [
            ("eso/prd/bot/prd/config", "jenkins-token"),
            ("eso/prd/bot/prd/config", "telegram-chat-id"),
        ]

    def test_an_override_naming_the_one_key_kind_leaves_the_other_key_unresolved(self):
        store = compliant_store()
        store["shared/ceph"].meta["key_user_key"] = "cephx"
        store["shared/ceph"].keys.add("legacy")
        assert found(store) == [("shared/ceph", "legacy")]
        assert messages(store) == [
            "shared/ceph: legacy: no kind resolves it: cephx does not "
            "own it and no key_legacy names one"
        ]

    def test_a_stale_override(self):
        store = compliant_store()
        store["shared/wifi"].meta["key_public"] = "none"
        assert found(store) == [("shared/wifi", "key_public")]
        assert "stale override" in messages(store)[0]

    def test_a_copy_whose_primary_leaf_or_key_is_missing(self):
        store = compliant_store()
        catalog = store["eso/prd/kc/prd/catalog"].meta
        catalog["key_client-id"] = "copy:eso/prd/gone/prd/oidc#client_id"
        catalog["key_client-secret"] = "copy:eso/prd/app/prd/oidc#secret"
        store["iac/copy"].meta["rotation_mechanism"] = "copy:eso/prd/app/prd/token#gone"
        assert found(store) == [
            ("eso/prd/kc/prd/catalog", "key_client-id"),
            ("eso/prd/kc/prd/catalog", "key_client-secret"),
            ("iac/copy", "rotation_mechanism"),
        ]

    def test_a_copy_of_a_none_key_is_valid(self):
        store = compliant_store()
        assert (
            store["eso/prd/kc/prd/catalog"].meta["key_client-id"]
            == "copy:eso/prd/app/prd/oidc#client_id"
        )
        assert found(store) == []

    def test_a_copy_of_a_copy_is_a_finding(self):
        store = compliant_store()
        store["shared/wifi"].meta["key_password"] = "copy:iac/copy#token"
        assert messages(store) == [
            "shared/wifi: key_password: copy of iac/copy#token, which is itself a copy"
        ]

    def test_a_malformed_interval_or_never_without_notes(self):
        for value in ("14", "2w", "0d", "14 d", "Never"):
            store = compliant_store()
            store["eso/prd/app/prd/token"].meta["rotation_interval"] = value
            assert found(store) == [("eso/prd/app/prd/token", "rotation_interval")], value
        store = compliant_store()
        store["eso/prd/app/prd/token"].meta["rotation_interval"] = "never"
        assert messages(store) == ["eso/prd/app/prd/token: rotation_interval: never without notes"]
        store["eso/prd/app/prd/token"].meta["notes"] = "  "
        assert found(store) == [("eso/prd/app/prd/token", "rotation_interval")]
        store["eso/prd/app/prd/token"].meta["notes"] = "why it never rotates"
        assert found(store) == []

    def test_a_per_key_interval_is_validated(self):
        cases = {
            "interval_bearer-token": ("3w", "is not <n>d or never"),
            "interval_api-key": ("never", None),
            "interval_gone": ("14d", "the leaf has no key 'gone'"),
        }
        for key, (value, message) in cases.items():
            store = compliant_store()
            store["eso/prd/trello/prd/trello"].meta[key] = value
            if message is None:
                assert found(store) == [], key
            else:
                assert found(store) == [("eso/prd/trello/prd/trello", key)], key
                assert message in messages(store)[0]

    def test_a_per_key_never_needs_the_leafs_notes(self):
        store = compliant_store()
        store["eso/prd/bot/prd/config"].meta["interval_telegram-bot-token"] = "never"
        assert messages(store) == [
            "eso/prd/bot/prd/config: interval_telegram-bot-token: never without the leaf's notes"
        ]

    def test_a_per_key_interval_on_a_copy_or_none_key(self):
        store = compliant_store()
        store["eso/prd/kc/prd/catalog"].meta["interval_client-secret"] = "14d"
        store["eso/prd/bot/prd/config"].meta["interval_telegram-chat-id"] = "365d"
        store["eso/prd/app/prd/oidc"].meta["interval_client_id"] = "14d"
        assert found(store) == [
            ("eso/prd/app/prd/oidc", "interval_client_id"),
            ("eso/prd/bot/prd/config", "interval_telegram-chat-id"),
            ("eso/prd/kc/prd/catalog", "interval_client-secret"),
        ]
        assert "is none, which takes no interval" in messages(store)[0]

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
            "argocd-sync:",
            "manual:",
            "manual: ",
            "",
            "eso, manual:x",
        ]
        for value in valid:
            store = compliant_store()
            store["eso/prd/app/prd/token"].meta["rotation_activate"] = value
            assert found(store) == [], value
        for value in invalid:
            store = compliant_store()
            store["eso/prd/app/prd/token"].meta["rotation_activate"] = value
            assert ("eso/prd/app/prd/token", "rotation_activate") in found(store), value

    def test_rotation_args_that_are_not_a_json_object_or_too_large(self):
        for value in ("{realm: homelab}", '["homelab"]', json.dumps({"x": "y" * 520})):
            store = compliant_store()
            store["eso/prd/app/prd/oidc"].meta["rotation_args"] = value
            assert found(store) == [("eso/prd/app/prd/oidc", "rotation_args")], value

    def test_an_expiry_that_is_not_an_iso_date(self):
        for key in ("rotation_expires_at", "rotator_expires_at"):
            for value in ("2027-13-01", "20270131", "soon", "2027-01-31T00:00:00"):
                store = compliant_store()
                store["jenkins/youtrack"].meta[key] = value
                assert found(store) == [("jenkins/youtrack", key)], value

    def test_a_stamp_that_is_not_an_iso_date(self):
        store = compliant_store()
        store["eso/prd/app/prd/token"].meta["rotated_at_token"] = "yesterday"
        store["eso/prd/app/prd/oidc"].meta["rotated_at_client_secret"] = "2026-10-05"
        assert found(store) == [("eso/prd/app/prd/token", "rotated_at_token")]

    def test_a_leaf_whose_keys_cannot_be_read_is_a_finding_and_its_metadata_still_checked(self):
        store = compliant_store()
        store["shared/wifi"].keys = None
        store["shared/wifi"].meta["rotation_interval"] = "2w"
        assert found(store) == [("shared/wifi", "(data)"), ("shared/wifi", "rotation_interval")]

    def test_an_unannotated_leaf_is_reported_once_per_missing_key(self):
        store = compliant_store()
        store["eso/prd/bot/prd/config"].meta = {"rotation": "coordinated"}
        assert found(store) == [
            ("eso/prd/bot/prd/config", "rotation_mechanism"),
            ("eso/prd/bot/prd/config", "rotation_activate"),
            ("eso/prd/bot/prd/config", "rotation_interval"),
        ]


class TestScope:
    """A key-level finding blocks the key; a leaf-level one the leaf and the primaries copied in."""

    def test_a_compliant_store_blocks_nothing(self):
        result = audit(compliant_store())
        assert result.blocked_leaves == set()
        assert result.blocked_keys == set()

    def test_a_key_level_finding_blocks_that_key_alone(self):
        store = compliant_store()
        store["eso/prd/trello/prd/trello"].meta["interval_bearer-token"] = "3w"
        store["eso/prd/app/prd/oidc"].keys.add("url")
        result = audit(store)
        assert result.blocked_leaves == set()
        assert result.blocked_keys == {
            ("eso/prd/trello/prd/trello", "bearer-token"),
            ("eso/prd/app/prd/oidc", "url"),
        }
        assert not result.blocked("eso/prd/trello/prd/trello", "api-key")
        assert not result.blocked("eso/prd/app/prd/oidc", "client_secret")

    def test_a_leaf_level_finding_blocks_the_leaf_and_every_primary_copied_into_it(self):
        store = compliant_store()
        store["eso/prd/kc/prd/catalog"].meta["rotation_activate"] = "restart"
        result = audit(store)
        assert result.blocked_leaves == {"eso/prd/kc/prd/catalog"}
        assert result.blocked_keys == {
            ("eso/prd/app/prd/oidc", "client_id"),
            ("eso/prd/app/prd/oidc", "client_secret"),
        }
        assert result.blocked("eso/prd/kc/prd/catalog", "jenkins-user")
        assert not result.blocked("eso/prd/app/prd/token", "token")

    def test_a_blocked_copy_leaf_by_its_default_kind_blocks_that_primary_key(self):
        store = compliant_store()
        del store["iac/copy"].meta["rotation_activate"]
        result = audit(store)
        assert result.blocked_keys == {("eso/prd/app/prd/token", "token")}

    def test_a_leaf_level_finding_on_a_primary_leaves_its_copies_leaves_unblocked(self):
        store = compliant_store()
        store["eso/prd/app/prd/token"].meta["rotation_interval"] = "2w"
        result = audit(store)
        assert result.blocked_leaves == {"eso/prd/app/prd/token"}
        assert not result.blocked("iac/copy", "token")


class TestNeverAndDue:
    def test_keys_that_never_rotate_are_listed(self):
        assert audit(compliant_store()).never == [
            ("eso/prd/trello/prd/trello", "api-key"),
            ("eso/prd/trello/prd/trello", "token"),
            ("shared/wifi", "password"),
        ]

    def test_with_no_stamp_every_unblocked_scheduled_key_is_due_oldest_first(self):
        store = compliant_store()
        store["eso/prd/app/prd/token"].meta["rotated_at_token"] = "2026-10-01"
        store["shared/ceph"].meta["rotation_interval"] = "2w"
        store["shared/wifi"].meta["rotation_expires_at"] = "soon"
        store["eso/prd/trello/prd/trello"].meta["interval_bearer-token"] = "3w"
        today = datetime.date(2026, 10, 20)
        due = due_keys(store, audit(store), today)
        assert [(s.leaf, s.key) for s in due] == [
            ("eso/prd/app/prd/oidc", "client_secret"),
            ("eso/prd/bot/prd/config", "jenkins-token"),
            ("eso/prd/bot/prd/config", "telegram-bot-token"),
            ("eso/prd/es/prd/creds", "password"),
            ("eso/prd/yt/prd/webhook", "token"),
            ("jenkins/youtrack", "admin-token"),
            ("eso/prd/app/prd/token", "token"),
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
        bao.leaves["shared/wifi"]["meta"]["rotation_mechanism"] = "wifi"
        run = Run(bao)
        assert run("audit") == 1
        assert (
            "shared/wifi: rotation_mechanism: unknown kind 'wifi': not a kind of design §6, "
            "none, or copy:<path>#<key>" in run.lines
        )
        assert "SECRET" not in run.text

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
        bao.leaves["rotator/staging/random/eso/prd/app/prd/token"] = {
            "data": {"token": "SECRET-staged"},
            "meta": {},
        }
        run = Run(bao)
        assert run("audit") == 0, run.text
        assert not [p for _, p, *_ in bao.requests if "rotator/lock" in p or "staging" in p]


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
            {
                "rotation_mechanism": "random",
                "rotation_interval": "14d",
                "rotation_activate": "none",
            },
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
            {
                "rotation_mechanism": "random",
                "rotation_interval": "14d",
                "rotation_activate": "auto",
            },
        )
        assert audit(store).findings == []

    def test_the_live_audit_reports_an_orphan(self):
        run = Run(annotated_bao(), cluster=self.without(("trello-prd", "trello")))
        assert run("audit") == 1
        assert run.lines[0].startswith("eso/prd/trello/prd/trello: (consumers): an orphan")
