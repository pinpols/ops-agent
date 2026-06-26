from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = ROOT / "deploy" / "k8s"
PROM_DIR = ROOT / "deploy" / "prometheus"


def _load_yaml_documents(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        docs = [doc for doc in yaml.safe_load_all(fh) if doc is not None]
    assert docs, f"{path} contains no YAML documents"
    assert all(isinstance(doc, dict) for doc in docs), f"{path} has non-object YAML docs"
    return docs


def test_k8s_manifests_are_parseable_objects() -> None:
    for path in sorted(K8S_DIR.glob("*.yaml")):
        for doc in _load_yaml_documents(path):
            assert doc.get("apiVersion"), f"{path} missing apiVersion"
            assert doc.get("kind"), f"{path} missing kind"
            if doc["kind"] == "Kustomization":
                continue
            metadata = doc.get("metadata")
            assert isinstance(metadata, dict), f"{path} missing metadata object"
            assert metadata.get("name"), f"{path} missing metadata.name"


def test_kustomization_resources_exist() -> None:
    kustomization = _load_yaml_documents(K8S_DIR / "kustomization.yaml")[0]
    resources = kustomization.get("resources")
    assert isinstance(resources, list) and resources
    for resource in resources:
        assert isinstance(resource, str)
        assert (K8S_DIR / resource).exists(), f"kustomization resource not found: {resource}"


def test_k8s_default_images_do_not_use_latest_tag() -> None:
    for path in sorted(K8S_DIR.glob("*.yaml")):
        for doc in _load_yaml_documents(path):
            spec = doc.get("spec", {})
            template = spec.get("template", {}) if isinstance(spec, dict) else {}
            pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
            containers = pod_spec.get("containers", []) if isinstance(pod_spec, dict) else []
            for container in containers:
                image = container.get("image", "")
                assert not image.endswith(":latest"), f"{path} uses mutable latest image tag"


def test_prometheus_alerts_are_parseable_and_actionable() -> None:
    rules_file = PROM_DIR / "ops-agent-alerts.yml"
    alert_config = _load_yaml_documents(rules_file)[0]
    groups = alert_config.get("groups")
    assert isinstance(groups, list) and groups

    alerts = []
    for group in groups:
        assert isinstance(group.get("name"), str) and group["name"]
        rules = group.get("rules")
        assert isinstance(rules, list) and rules
        alerts.extend(rules)

    assert alerts
    for rule in alerts:
        assert isinstance(rule.get("alert"), str) and rule["alert"]
        assert isinstance(rule.get("expr"), str) and rule["expr"].strip()
        labels = rule.get("labels")
        annotations = rule.get("annotations")
        assert isinstance(labels, dict), f"{rule['alert']} missing labels"
        assert labels.get("severity") in {"warning", "critical"}
        assert isinstance(annotations, dict), f"{rule['alert']} missing annotations"
        assert annotations.get("summary"), f"{rule['alert']} missing summary"
