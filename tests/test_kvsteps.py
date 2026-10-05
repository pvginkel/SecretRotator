"""The generic KV steps (design §4.2): random.generate's default and shape, kv.write and kv.copy
as KV v2 patches with read-back and an undo to the version they started from, and kv.stamp's one
metadata patch of ISO-dated per-key stamps."""

import pytest
from fixtures import compliant_store
from plans import COPY, LEAF, NOW, client, fake

from secret_rotator.audit import audit
from secret_rotator.kvsteps import URLSAFE, KvCopy, KvStamp, KvWrite, RandomGenerate
from secret_rotator.model import StepFailed, value_name
from secret_rotator.openbao import OpenBaoError

TRELLO = "eso/prd/trello/prd/trello"  # api-key, bearer-token, token


class Ctx:
    def __init__(self, bao, **staged):
        self.bao = client(bao)
        self.now = NOW
        self.values = dict(staged)

    def progress(self, detail):
        pass

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)

    def ask(self, request):
        raise AssertionError("a tool step asked the operator")


class TestRandomGenerate:
    def test_its_default_is_43_url_safe_characters_staged_under_the_key(self):
        ctx = Ctx(fake())
        assert RandomGenerate("token").run(ctx) == "43 characters"
        value = ctx.values[value_name("token")]
        assert len(value) == 43 and set(value) <= set(URLSAFE)

    def test_it_draws_a_new_value_each_plan(self):
        values = set()
        for _ in range(5):
            ctx = Ctx(fake())
            RandomGenerate("token").run(ctx)
            values.add(ctx.values[value_name("token")])
        assert len(values) == 5

    def test_a_re_run_keeps_the_value_already_staged(self):
        ctx = Ctx(fake(), **{value_name("token"): "staged"})
        RandomGenerate("token").run(ctx)
        assert ctx.values[value_name("token")] == "staged"

    def test_charset_and_length_set_the_shape(self):
        ctx = Ctx(fake())
        RandomGenerate("pin", length=8, charset="0123456789").run(ctx)
        assert ctx.values[value_name("pin")].isdigit() and len(ctx.values[value_name("pin")]) == 8

    @pytest.mark.parametrize("length, charset", [(0, URLSAFE), (8, "a"), (8, "abca"), (8, "")])
    def test_a_shape_that_cannot_be_drawn_fairly_is_refused(self, length, charset):
        with pytest.raises(ValueError):
            RandomGenerate("k", length=length, charset=charset)

    def test_it_is_silent_and_does_not_mutate(self):
        step = RandomGenerate("token")
        assert step.silent and not step.mutates and step.estimate == 0


class TestKvWrite:
    def test_it_patches_only_the_plan_s_keys_with_check_and_set(self):
        bao = fake()
        before = dict(bao.data(TRELLO))
        ctx = Ctx(bao, **{value_name("bearer-token"): "NEW"})
        assert KvWrite(TRELLO, ("bearer-token",)).run(ctx) == "v1 → v2"
        assert bao.data(TRELLO) == before | {"bearer-token": "NEW"}
        (_, _, _, body, ctype) = next(r for r in bao.requests if r[0] == "PATCH")
        assert body == {"data": {"bearer-token": "NEW"}, "options": {"cas": 1}}
        assert ctype == "application/merge-patch+json"

    def test_it_stages_the_version_it_starts_from_before_it_writes(self):
        bao = fake()
        ctx = Ctx(bao, **{value_name("token"): "NEW"})
        KvWrite(LEAF, ("token",)).run(ctx)
        assert ctx.values["kv.write:from"] == "1"

    def test_a_write_that_already_landed_is_a_no_op(self):
        bao = fake()
        ctx = Ctx(bao, **{value_name("token"): "NEW"})
        step = KvWrite(LEAF, ("token",))
        step.run(ctx)
        assert step.run(ctx) == "v1 → v2"
        assert len([r for r in bao.requests if r[0] == "PATCH"]) == 1

    def test_a_read_back_that_differs_fails(self):
        bao = fake()
        real = bao.patch_data
        bao.patch_data = lambda leaf, body, *a: real(leaf, body | {"data": {"token": "X"}}, *a)
        with pytest.raises(StepFailed, match="read-back"):
            KvWrite(LEAF, ("token",)).run(Ctx(bao, **{value_name("token"): "NEW"}))

    def test_a_key_without_a_staged_value_fails_before_anything_is_written(self):
        bao = fake()
        with pytest.raises(StepFailed, match="no new value is staged for token"):
            KvWrite(LEAF, ("token",)).run(Ctx(bao))
        assert bao.writes() == []

    def test_a_concurrent_write_is_refused_by_check_and_set(self):
        bao = fake()
        real = bao.get_data

        def racing(leaf, body, query, req):
            answer = real(leaf, body, query, req)
            bao.leaves[LEAF]["version"] = 7
            return answer

        bao.get_data = racing
        with pytest.raises(OpenBaoError, match="check-and-set"):
            KvWrite(LEAF, ("token",)).run(Ctx(bao, **{value_name("token"): "NEW"}))

    def test_its_undo_restores_the_values_of_the_version_it_started_from(self):
        bao = fake()
        before = dict(bao.data(TRELLO))
        ctx = Ctx(bao, **{value_name("bearer-token"): "NEW"})
        step = KvWrite(TRELLO, ("bearer-token",))
        step.run(ctx)
        bao.leaves[TRELLO]["data"]["api-key"] = "changed meanwhile"
        assert step.undo(ctx) == "v1's values back as v3"
        assert bao.data(TRELLO) == before | {"api-key": "changed meanwhile"}

    def test_its_undo_leaves_a_write_that_did_not_land_as_it_is(self):
        bao = fake()
        step = KvWrite(LEAF, ("token",))
        assert step.undo(Ctx(bao)) == "nothing was written"
        ctx = Ctx(bao, **{"kv.write:from": "1"})
        assert step.undo(ctx) == "holds v1's values"
        assert bao.writes() == []

    def test_its_undo_fails_when_the_version_it_started_from_is_gone(self):
        bao = fake()
        ctx = Ctx(bao, **{value_name("token"): "NEW"})
        step = KvWrite(LEAF, ("token",))
        step.run(ctx)
        bao.leaves[LEAF]["history"].clear()
        with pytest.raises(StepFailed, match="version 1 of"):
            step.undo(ctx)


class TestKvCopy:
    def test_it_patches_the_one_copy_key_with_the_primary_s_new_value(self):
        bao = fake()
        ctx = Ctx(bao, **{value_name("token"): "NEW"})
        step = KvCopy(COPY, "token", "token")
        assert step.id == f"kv.copy:{COPY}#token" and step.mutates
        step.run(ctx)
        assert bao.data(COPY) == {"token": "NEW"}
        assert step.undo(ctx) == "v1's values back as v3"
        assert bao.data(COPY)["token"].startswith("SECRET-")


class TestKvStamp:
    def test_it_stamps_the_rotated_keys_and_the_status_in_one_metadata_patch(self):
        bao = fake()
        bao.meta(TRELLO)["rotator_step"] = "kv.stamp"
        step = KvStamp(TRELLO, ("bearer-token",))
        assert step.silent and not step.mutates and step.undo is None
        step.run(Ctx(bao))
        (write,) = bao.writes()
        assert write[:2] == ("PATCH", f"kv/metadata/{TRELLO}")
        meta = bao.meta(TRELLO)
        assert meta["rotated_at_bearer-token"] == "2026-10-05"
        assert "rotated_at_token" not in meta and "rotated_at_api-key" not in meta
        assert meta["rotator_status"] == "ok" and "rotator_step" not in meta
        assert meta["rotator_last_run"] == "2026-10-05T04:30:00+00:00"

    def test_it_clears_the_nightly_run_s_backoff(self):
        bao = fake()
        bao.meta(TRELLO).update({"rotator_failed_nights": "3", "rotator_held_by": "ANS-9"})
        KvStamp(TRELLO, ("bearer-token",)).run(Ctx(bao))
        assert "rotator_failed_nights" not in bao.meta(TRELLO)
        assert "rotator_held_by" not in bao.meta(TRELLO)

    def test_the_audit_accepts_its_stamps(self):
        bao = fake()
        KvStamp(TRELLO, ("bearer-token",)).run(Ctx(bao))
        store = compliant_store()
        store[TRELLO].meta.update(bao.meta(TRELLO))
        assert audit(store).findings == []

    def test_a_leaf_gone_mid_plan_is_a_failure_not_a_stamp(self):
        bao = fake()
        del bao.leaves[TRELLO]
        with pytest.raises(OpenBaoError, match="HTTP 404: no leaf"):
            KvStamp(TRELLO, ("bearer-token",)).run(Ctx(bao))
