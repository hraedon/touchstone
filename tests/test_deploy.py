"""Static contracts for the deliberately small public deployment surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
K8S = ROOT / "deploy" / "k8s"
SECRET_NAME = "touchstone-sink-secrets"
REQUIRED_SECRET_KEYS = {
    "TOUCHSTONE_DSN",
    "VERIFICATION_TOKEN",
    "ENDPOINT_URL",
    "EBAY_CLIENT_ID",
    "EBAY_CLIENT_SECRET",
}
RELEASE_IMAGE = (
    "ghcr.io/hraedon/touchstone@"
    "sha256:0fb4820a0ea66bd7323f50983bbe20e6db5889da9536017ecbd6f9e241fa153c"
)


def _manifest(name: str) -> dict[str, Any]:
    documents = list(yaml.safe_load_all((K8S / name).read_text(encoding="utf-8")))
    assert len(documents) == 1
    document = documents[0]
    assert isinstance(document, dict)
    return document


def test_exactly_four_tracked_manifests_define_the_sink_surface() -> None:
    assert {path.name for path in K8S.glob("*.yaml")} == {
        "namespace.yaml",
        "deployment.yaml",
        "service.yaml",
        "ingress.yaml",
    }


def test_deployment_requires_every_secret_and_runs_hardened() -> None:
    deployment = _manifest("deployment.yaml")
    assert deployment["kind"] == "Deployment"
    assert deployment["metadata"] == {"name": "touchstone-sink", "namespace": "touchstone"}
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
    }

    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert "imagePullSecrets" not in pod
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}

    [container] = pod["containers"]
    assert container["image"] == RELEASE_IMAGE
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["ports"] == [{"name": "http", "containerPort": 8080}]
    assert {entry["name"] for entry in container["env"]} == REQUIRED_SECRET_KEYS
    for entry in container["env"]:
        assert entry["valueFrom"]["secretKeyRef"] == {
            "name": SECRET_NAME,
            "key": entry["name"],
        }

    security = container["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"] == {"drop": ["ALL"]}
    assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert container["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert set(container["resources"]) == {"requests", "limits"}

    [migration] = pod["initContainers"]
    assert migration["image"] == container["image"]
    assert migration["imagePullPolicy"] == "IfNotPresent"
    assert migration["command"] == ["alembic", "upgrade", "head"]
    assert migration["env"] == [
        {
            "name": "TOUCHSTONE_DSN",
            "valueFrom": {
                "secretKeyRef": {"name": SECRET_NAME, "key": "TOUCHSTONE_DSN"}
            },
        }
    ]
    assert migration["securityContext"] == security


def test_service_is_cluster_internal() -> None:
    service = _manifest("service.yaml")
    assert service["kind"] == "Service"
    assert service["metadata"] == {"name": "touchstone-sink", "namespace": "touchstone"}
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 8080, "targetPort": "http"}
    ]


def test_external_ingress_exposes_only_the_exact_callback_path() -> None:
    ingress = _manifest("ingress.yaml")
    assert ingress["kind"] == "Ingress"
    assert ingress["metadata"]["annotations"] == {
        "cert-manager.io/cluster-issuer": "letsencrypt-prod-porkbun",
        "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
    }
    spec = ingress["spec"]
    assert spec["ingressClassName"] == "traefik-external"
    assert spec["tls"] == [
        {"hosts": ["ebdel.hraedon.com"], "secretName": "ebdel-hraedon-com-tls"}
    ]
    [rule] = spec["rules"]
    assert rule["host"] == "ebdel.hraedon.com"
    assert rule["http"]["paths"] == [
        {
            "path": "/",
            "pathType": "Exact",
            "backend": {
                "service": {"name": "touchstone-sink", "port": {"name": "http"}}
            },
        }
    ]


def test_namespace_pins_the_cluster_pod_security_version() -> None:
    namespace = _manifest("namespace.yaml")
    assert namespace["metadata"]["labels"] == {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "v1.35",
        "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/audit-version": "v1.35",
        "pod-security.kubernetes.io/warn": "restricted",
        "pod-security.kubernetes.io/warn-version": "v1.35",
    }
