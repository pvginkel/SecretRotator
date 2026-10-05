"""The Kubernetes API client: the ServiceAccount's bearer token on every request, a 404 as no
object, every other refusal and a transport failure as KubeError."""

import pytest
from fake_cluster import TOKEN, FakeCluster

from secret_rotator.kube import Kube, KubeError

DEPLOYMENT = "/apis/apps/v1/namespaces/app-prd/deployments/app"


def test_it_sends_the_token_and_reads_an_object():
    cluster = FakeCluster()
    assert cluster.kube().get(DEPLOYMENT)["metadata"]["name"] == "app"


def test_no_such_object_is_none():
    assert FakeCluster().kube().get("/apis/apps/v1/namespaces/app-prd/deployments/gone") is None


def test_a_list_it_serves_is_its_items_and_one_it_does_not_an_error():
    kube = FakeCluster().kube()
    assert {o["metadata"]["name"] for o in kube.items("/apis/apps/v1/daemonsets")} == {"app-agent"}
    cluster = FakeCluster([])
    kube = Kube(TOKEN, opener=lambda req: cluster.answer(404, {"message": "not found"}))
    with pytest.raises(KubeError, match="the API does not serve it") as e:
        kube.items("/apis/external-secrets.io/v1/externalsecrets")
    assert e.value.status == 404


def test_a_refusal_names_the_request_and_the_apiserver_s_message():
    with pytest.raises(KubeError) as e:
        FakeCluster().kube("SECRET-another-token").get(DEPLOYMENT)
    assert str(e.value) == f"GET {DEPLOYMENT}: HTTP 401: Unauthorized" and e.value.status == 401
    assert "SECRET" not in str(e.value)


def test_a_transport_failure_is_a_kube_error_without_status():
    cluster = FakeCluster()
    cluster.broken["GET", DEPLOYMENT] = TimeoutError("timed out")
    with pytest.raises(KubeError, match=f"GET {DEPLOYMENT}: transport error") as e:
        cluster.kube().get(DEPLOYMENT)
    assert e.value.status is None


def test_a_merge_patch_returns_the_patched_object_and_refuses_no_object():
    cluster = FakeCluster()
    kube = cluster.kube()
    patched = kube.merge_patch(DEPLOYMENT, {"metadata": {"annotations": {"a": "b"}}})
    assert patched["metadata"]["annotations"]["a"] == "b"
    assert cluster.get("deployments", "app-prd", "app")["metadata"]["annotations"]["a"] == "b"
    with pytest.raises(KubeError, match="HTTP 404: no such object"):
        kube.merge_patch(DEPLOYMENT + "-gone", {"metadata": {}})
