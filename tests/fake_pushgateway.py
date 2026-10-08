"""The Prometheus Pushgateway behind the prd apiserver's service proxy, for fake_cluster: a PUT
replaces its group, as Pushgateway 1.x does, and is answered 200 with no body. It reads the body as
the text exposition format and refuses with a text answer, as the real one answers 400, a sample
before its TYPE line, a metric's samples apart, a sample twice, a label value escaped wrongly, or a
job or instance label, which the group's URL sets. With no endpoints, the apiserver answers 503."""

import json
import re

SERVICE = "prometheus-prd-prometheus-pushgateway:9091"
PUSHGATEWAY = f"/api/v1/namespaces/prometheus-prd/services/{SERVICE}"
GROUPS = f"{PUSHGATEWAY}/proxy/metrics/job/secret-rotator/instance/"
INVALID = "pushed metrics are invalid or inconsistent with existing metrics"
SAMPLE = re.compile(r"(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})? (?P<value>\S+)")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\[\\"n])*)"(?:,|$)')
UNESCAPED = {"\\": "\\", '"': '"', "n": "\n"}


class Refused(Exception):
    pass


def labels_of(text):
    labels, at = [], 0
    while at < len(text):
        found = LABEL.match(text, at)
        if found is None:
            raise Refused(f"text format parsing error: invalid label set {text!r}")
        value = re.sub(r"\\(.)", lambda m: UNESCAPED[m[1]], found[2])
        labels.append((found[1], value))
        at = found.end()
    names = [name for name, _ in labels]
    if len(set(names)) != len(names) or {"job", "instance"} & set(names):
        raise Refused(f"invalid label set {text!r}")
    return tuple(sorted(labels))


def parse(text):
    """The samples, (name, sorted label pairs) -> value; Refused for what the real one refuses."""
    samples, typed, ended, current = {}, set(), set(), None
    for line in text.splitlines():
        if line.startswith("# HELP "):
            continue
        if line.startswith("# TYPE "):
            name, kind = line.removeprefix("# TYPE ").split(" ")
            if name in typed or kind != "gauge":
                raise Refused(f"text format parsing error: TYPE line {line!r}")
            typed.add(name)
            continue
        found = SAMPLE.fullmatch(line)
        if found is None:
            raise Refused(f"text format parsing error: {line!r}")
        name = found["name"]
        if name not in typed:
            raise Refused(f"text format parsing error: {name} before its TYPE line")
        if name != current:
            if name in ended:
                raise Refused(f"text format parsing error: second block of {name}")
            ended.add(current)
            current = name
        key = (name, labels_of(found["labels"] or ""))
        if key in samples:
            raise Refused(f"{name} was collected before with the same name and label values")
        samples[key] = float(found["value"])
    return samples


class FakePushgateway:
    def __init__(self):
        self.groups = {}  # instance -> its samples, as parse() reads them
        self.bodies = {}  # instance -> the text it was pushed
        self.down = False  # the Service has no endpoints

    def put(self, path, text):
        """(status, the answer as bytes) to a PUT on the path under the apiserver's proxy."""
        assert path.startswith(GROUPS), path
        if self.down:
            message = f'no endpoints available for service "{SERVICE}"'
            status = {"kind": "Status", "status": "Failure", "message": message, "code": 503}
            return 503, json.dumps(status).encode()
        group = path.removeprefix(GROUPS)
        try:
            samples = parse(text)
        except Refused as e:
            return 400, f"{INVALID}: {e}\n".encode()
        self.groups[group] = samples
        self.bodies[group] = text
        return 200, b""

    def value(self, group, name, **labels):
        """The sample's value; None when the group holds no such sample."""
        return self.groups[group].get((name, tuple(sorted(labels.items()))))

    def of(self, group, name):
        """Every sample of the metric in the group: (its labels as a dict, its value)."""
        return [
            (dict(labels), value) for (n, labels), value in self.groups[group].items() if n == name
        ]
