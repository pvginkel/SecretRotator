"""Design §3.5's dashboard, dashboards/secret-rotation.json, as SecretRotator's build publishes it
with grafanaDashboards.publish: a bare dashboard model under its fixed uid, its Prometheus picked
through the dashboard variable, and queries over exactly the series the rotator pushes
(metrics.HELP), so a broken dashboard fails the Test stage before the publish stage runs."""

import json
import re
from pathlib import Path

import pytest

from secret_rotator import metrics

DASHBOARDS = Path(__file__).resolve().parents[1] / "dashboards"
DATASOURCE = {"type": "prometheus", "uid": "${datasource}"}

STRINGS = re.compile(r'"(?:[^"\\]|\\.)*"')
MATCHERS = re.compile(r"\{[^}]*\}")
GROUPINGS = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
RANGES = re.compile(r"\[[^\]]*\]")
# An identifier no call follows and no digit, dot or $ precedes: a metric name or a keyword.
NAME = re.compile(r"(?<![\w:$.])([a-zA-Z_:][\w:]*)(?![\w:])(?!\s*\()")
KEYWORDS = {"and", "or", "unless", "bool", "offset", "atan2", "inf", "nan"}

# What design §3.5 has the dashboard show, by the panel that shows it.
SHOWN = {
    "Keys by due date": {
        "secret_rotation_due_timestamp",
        "secret_rotation_last_rotated_timestamp",
        "secret_rotation_key_info",
    },
    "Keys never stamped": {"secret_rotation_key_info"},
    "Leaf statuses": {"secret_rotation_status"},
    "Audit findings": {"secret_rotation_finding"},
    "Since the last nightly run": {"secret_rotator_run_timestamp"},
    "Last run": {"secret_rotator_run_success"},
    "Run duration": {"secret_rotator_run_duration_seconds"},
    "Rotations a night": {"secret_rotator_run_rotations", "secret_rotator_run_deferred"},
    "Dry run": {"secret_rotator_dry_run"},
    "Paused": {"secret_rotator_paused"},
}


def series(expr: str) -> set[str]:
    """The metric names a PromQL expression selects."""
    rest = STRINGS.sub('""', expr)
    for pattern in (MATCHERS, GROUPINGS, RANGES):
        rest = pattern.sub(" ", rest)
    return {name for name in NAME.findall(rest) if name not in KEYWORDS}


def walk(node):
    """Every object in the model, the model first."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def panels(model):
    """Every panel, a collapsed row's own included."""
    for panel in model["panels"]:
        yield panel
        yield from panel.get("panels", [])


def queried(panel) -> set[str]:
    return set().union(*(series(t["expr"]) for t in panel.get("targets", [])))


@pytest.fixture(scope="module")
def dashboard():
    return json.loads((DASHBOARDS / "secret-rotation.json").read_text())


def test_series_are_the_metric_names_an_expression_selects():
    expr = (
        'count(max by (leaf, key) (a_b{job="x", m=~"y(z)}"}[1d] offset 2h) > 1e3) '
        "or vector(0) unless on (leaf) c:d and ignoring (e) f[$__range]"
    )
    assert series(expr) == {"a_b", "c:d", "f"}


def test_every_file_the_publish_step_reads_is_a_model_with_a_uid_of_its_own():
    uids = []
    for path in sorted(DASHBOARDS.glob("*.json")):
        model = json.loads(path.read_text())
        assert isinstance(model, dict), path.name
        assert isinstance(model.get("uid"), str) and model["uid"], path.name
        uids.append(model["uid"])
    assert uids
    assert len(set(uids)) == len(uids)


def test_its_uid_and_title_are_fixed(dashboard):
    assert (dashboard["uid"], dashboard["title"]) == ("secret-rotation", "Secret rotation")
    assert "id" not in dashboard


def test_it_picks_its_prometheus_through_the_datasource_variable(dashboard):
    [variable] = [v for v in dashboard["templating"]["list"] if v["type"] == "datasource"]
    assert (variable["name"], variable["query"], variable["current"]) == (
        "datasource",
        "prometheus",
        {},
    )
    for panel in panels(dashboard):
        if panel.get("targets"):
            assert panel["datasource"] == DATASOURCE, panel["title"]
            assert all(t["datasource"] == DATASOURCE for t in panel["targets"]), panel["title"]
    assert all(node["datasource"] == DATASOURCE for node in walk(dashboard) if "datasource" in node)
    uids = [node["uid"] for node in walk(dashboard) if "uid" in node]
    assert uids[0] == "secret-rotation"
    assert set(uids[1:]) == {"${datasource}"}


def test_it_queries_every_series_the_rotator_pushes_and_no_other(dashboard):
    exprs = [t["expr"] for panel in panels(dashboard) for t in panel.get("targets", [])]
    assert all(series(expr) for expr in exprs)
    assert set().union(*map(series, exprs)) == set(metrics.HELP)
    assert not any("__name__" in expr for expr in exprs)


def test_it_shows_what_design_3_5_names(dashboard):
    by_title = {p["title"]: p for p in panels(dashboard) if p["type"] != "row"}
    assert {title: queried(by_title[title]) for title in SHOWN if title in by_title} == SHOWN
    never = by_title["Keys never stamped"]["targets"]
    assert all('stamped="false"' in t["expr"] for t in never)
    # One sample a night, kept until the next push: a range shows each night's count.
    assert all(t["range"] for t in by_title["Rotations a night"]["targets"])
