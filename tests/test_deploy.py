"""Static contracts for the deployment surface.

These catch the class of mistake that only shows up in a cluster: a workload wired to
the wrong Secret, a container that lost its hardening in an edit, a destructive
CronJob that quietly became unsuspended, or a UI that acquired the eBay credentials
it must never hold.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
K8S = ROOT / "deploy" / "k8s"

RELEASE_IMAGE = (
    "ghcr.io/hraedon/touchstone@"
    "sha256:dfbe04040da7fd816a5bd467f4521acc2f7d24e4dbfb842af323212eeef15b84"
)

# Every value lives in exactly one Secret, and every workload gets only what it uses.
DB_SECRET = "touchstone-db-secrets"
EBAY_SECRET = "touchstone-ebay-secrets"
SINK_SECRET = "touchstone-sink-secrets"
EXTRACT_SECRET = "touchstone-extract-secrets"
WEB_SECRET = "touchstone-web-secrets"

TRACKED_MANIFESTS = {
    "namespace.yaml",
    "deployment.yaml",
    "service.yaml",
    "ingress.yaml",
    "deployment-web.yaml",
    "service-web.yaml",
    "ingress-web.yaml",
    "cronjob-scanner.yaml",
    "cronjob-extractor.yaml",
    "cronjob-prune.yaml",
}


def _manifest(name: str) -> dict[str, Any]:
    documents = list(yaml.safe_load_all((K8S / name).read_text(encoding="utf-8")))
    assert len(documents) == 1
    document = documents[0]
    assert isinstance(document, dict)
    return document


def _secret_refs(container: dict[str, Any]) -> dict[str, tuple[str, str]]:
    refs: dict[str, tuple[str, str]] = {}
    for entry in container.get("env", []):
        source = entry.get("valueFrom", {}).get("secretKeyRef")
        if source is not None:
            refs[entry["name"]] = (source["name"], source["key"])
    return refs


def _assert_hardened(container: dict[str, Any]) -> None:
    security = container["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"] == {"drop": ["ALL"]}
    assert set(container["resources"]) == {"requests", "limits"}


def _assert_restricted_pod(pod: dict[str, Any]) -> None:
    assert pod["automountServiceAccountToken"] is False
    assert "imagePullSecrets" not in pod
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}


def test_the_tracked_manifests_are_exactly_the_deployment_surface() -> None:
    assert {path.name for path in K8S.glob("*.yaml")} == TRACKED_MANIFESTS


def test_every_workload_is_pinned_to_one_immutable_digest() -> None:
    """A mutable tag never rolls itself: `imagePullPolicy: Always` only applies at
    pod creation, so CI can be green for weeks while a pod serves a stale build."""
    images: list[str] = []
    for name in TRACKED_MANIFESTS:
        document = _manifest(name)
        if document["kind"] == "Deployment":
            spec = document["spec"]["template"]["spec"]
        elif document["kind"] == "CronJob":
            spec = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        else:
            continue
        for container in spec.get("initContainers", []) + spec["containers"]:
            images.append(container["image"])
            assert container["imagePullPolicy"] == "IfNotPresent"
    assert images, "no workloads found — the matcher has drifted"
    assert set(images) == {RELEASE_IMAGE}


class TestSink:
    def test_it_runs_hardened_and_reads_only_what_it_needs(self) -> None:
        deployment = _manifest("deployment.yaml")
        assert deployment["metadata"] == {
            "name": "touchstone-sink",
            "namespace": "touchstone",
        }
        assert deployment["spec"]["replicas"] == 1
        assert deployment["spec"]["strategy"] == {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
        }
        pod = deployment["spec"]["template"]["spec"]
        _assert_restricted_pod(pod)

        [container] = pod["containers"]
        _assert_hardened(container)
        assert container["ports"] == [{"name": "http", "containerPort": 8080}]
        assert _secret_refs(container) == {
            "TOUCHSTONE_DSN": (DB_SECRET, "TOUCHSTONE_DSN"),
            "VERIFICATION_TOKEN": (SINK_SECRET, "VERIFICATION_TOKEN"),
            "ENDPOINT_URL": (SINK_SECRET, "ENDPOINT_URL"),
            "EBAY_CLIENT_ID": (EBAY_SECRET, "EBAY_CLIENT_ID"),
            "EBAY_CLIENT_SECRET": (EBAY_SECRET, "EBAY_CLIENT_SECRET"),
        }
        assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
        assert container["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}

    def test_only_the_sink_migrates_the_database(self) -> None:
        """Two workloads racing `alembic upgrade head` is a deadlock waiting to happen."""
        migrating: list[str] = []
        for name in TRACKED_MANIFESTS:
            document = _manifest(name)
            if document["kind"] == "Deployment":
                spec = document["spec"]["template"]["spec"]
            elif document["kind"] == "CronJob":
                spec = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            else:
                continue
            for container in spec.get("initContainers", []) + spec["containers"]:
                if "alembic" in (container.get("command") or []):
                    migrating.append(f"{name}/{container['name']}")
        assert migrating == ["deployment.yaml/migrate"]

    def test_the_migration_container_matches_the_service_hardening(self) -> None:
        pod = _manifest("deployment.yaml")["spec"]["template"]["spec"]
        [migration] = pod["initContainers"]
        [container] = pod["containers"]
        assert migration["command"] == ["alembic", "upgrade", "head"]
        assert migration["securityContext"] == container["securityContext"]
        assert _secret_refs(migration) == {"TOUCHSTONE_DSN": (DB_SECRET, "TOUCHSTONE_DSN")}

    def test_the_external_ingress_exposes_only_the_exact_callback_path(self) -> None:
        ingress = _manifest("ingress.yaml")
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

    def test_the_service_is_cluster_internal(self) -> None:
        service = _manifest("service.yaml")
        assert service["spec"]["type"] == "ClusterIP"
        assert service["spec"]["ports"] == [
            {"name": "http", "port": 8080, "targetPort": "http"}
        ]


class TestWeb:
    def test_the_ui_cannot_reach_ebay(self) -> None:
        """The strongest form of "a page load must not spend API budget".

        The rule is enforced in code by an architecture test; this makes it a
        property of the deployment too, so it survives a route module that imports
        something it shouldn't.
        """
        [container] = _manifest("deployment-web.yaml")["spec"]["template"]["spec"]["containers"]
        refs = _secret_refs(container)
        assert "EBAY_CLIENT_ID" not in refs
        assert "EBAY_CLIENT_SECRET" not in refs
        assert EBAY_SECRET not in {secret for secret, _ in refs.values()}

    def test_it_reads_exactly_the_database_and_its_session_key(self) -> None:
        [container] = _manifest("deployment-web.yaml")["spec"]["template"]["spec"]["containers"]
        assert _secret_refs(container) == {
            "TOUCHSTONE_DSN": (DB_SECRET, "TOUCHSTONE_DSN"),
            "TOUCHSTONE_SECRET_KEY": (WEB_SECRET, "TOUCHSTONE_SECRET_KEY"),
        }

    def test_it_runs_hardened_and_serves_the_app_factory(self) -> None:
        pod = _manifest("deployment-web.yaml")["spec"]["template"]["spec"]
        _assert_restricted_pod(pod)
        [container] = pod["containers"]
        _assert_hardened(container)
        assert container["command"][:3] == ["uvicorn", "--factory", "touchstone.web.app:create_app"]
        assert container["ports"] == [{"name": "http", "containerPort": 8080}]

    def test_liveness_does_not_depend_on_the_database_but_readiness_does(self) -> None:
        """Conflating them turns a Postgres blip into a crash loop."""
        [container] = _manifest("deployment-web.yaml")["spec"]["template"]["spec"]["containers"]
        assert container["livenessProbe"]["httpGet"]["path"] == "/livez"
        assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"

    def test_the_internal_ingress_serves_the_whole_app_on_the_internal_class(self) -> None:
        ingress = _manifest("ingress-web.yaml")
        spec = ingress["spec"]
        assert spec["ingressClassName"] == "traefik-internal"
        assert spec["tls"] == [
            {
                "hosts": ["touchstone.k8s.hraedon.com"],
                "secretName": "touchstone-k8s-hraedon-com-tls",
            }
        ]
        [rule] = spec["rules"]
        assert rule["host"] == "touchstone.k8s.hraedon.com"
        assert rule["http"]["paths"] == [
            {
                "path": "/",
                "pathType": "Prefix",
                "backend": {
                    "service": {"name": "touchstone-web", "port": {"name": "http"}}
                },
            }
        ]

    def test_the_ui_is_not_reachable_from_the_internet(self) -> None:
        """`*.k8s.hraedon.com` on traefik-internal is the estate's LAN convention."""
        for name in ("ingress-web.yaml",):
            spec = _manifest(name)["spec"]
            assert spec["ingressClassName"] != "traefik-external"
            for rule in spec["rules"]:
                assert rule["host"].endswith(".k8s.hraedon.com")


class TestScheduledWork:
    def _cronjob(self, name: str) -> dict[str, Any]:
        document = _manifest(name)
        assert document["kind"] == "CronJob"
        return document

    def test_the_scanner_runs_tick_and_never_overlaps_itself(self) -> None:
        spec = self._cronjob("cronjob-scanner.yaml")["spec"]
        assert spec["concurrencyPolicy"] == "Forbid"
        assert spec["suspend"] is False
        job = spec["jobTemplate"]["spec"]
        # A stuck scan must not be retried: retrying spends more of a finite
        # allowance on the same problem.
        assert job["backoffLimit"] == 0
        assert job["activeDeadlineSeconds"] > 0
        pod = job["template"]["spec"]
        assert pod["restartPolicy"] == "Never"
        _assert_restricted_pod(pod)
        [container] = pod["containers"]
        _assert_hardened(container)
        assert container["command"] == ["touchstone", "tick"]
        assert _secret_refs(container) == {
            "TOUCHSTONE_DSN": (DB_SECRET, "TOUCHSTONE_DSN"),
            "EBAY_CLIENT_ID": (EBAY_SECRET, "EBAY_CLIENT_ID"),
            "EBAY_CLIENT_SECRET": (EBAY_SECRET, "EBAY_CLIENT_SECRET"),
        }

    def test_the_extractor_is_a_separate_job_from_the_scanner(self) -> None:
        """A scan must complete whether or not the model provider is reachable."""
        scanner = self._cronjob("cronjob-scanner.yaml")
        extractor = self._cronjob("cronjob-extractor.yaml")
        assert scanner["spec"]["schedule"] != extractor["spec"]["schedule"]
        assert extractor["spec"]["suspend"] is False

        [container] = extractor["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        assert container["command"] == ["touchstone", "extract"]
        assert _secret_refs(container) == {
            "TOUCHSTONE_DSN": (DB_SECRET, "TOUCHSTONE_DSN"),
            "UMANS_API_KEY": (EXTRACT_SECRET, "UMANS_API_KEY"),
        }
        # The extractor has no business holding the session key or the eBay keyset.
        assert {secret for secret, _ in _secret_refs(container).values()} == {
            DB_SECRET,
            EXTRACT_SECRET,
        }

    def test_a_missing_model_key_degrades_rather_than_stopping_extraction(self) -> None:
        """`optional: true` is load-bearing, not laziness.

        Without a key `run_extraction` uses the regex fast path and leaves the rest
        unresolved. A required secretKeyRef would stop the pod starting instead,
        which converts "no model available" into "extraction stopped running" — a
        worse failure, and a silent one.
        """
        [container] = self._cronjob("cronjob-extractor.yaml")["spec"]["jobTemplate"]["spec"][
            "template"
        ]["spec"]["containers"]
        [umans] = [e for e in container["env"] if e["name"] == "UMANS_API_KEY"]
        assert umans["valueFrom"]["secretKeyRef"]["optional"] is True

        # The database, by contrast, is not optional anywhere.
        for name in ("cronjob-scanner.yaml", "cronjob-extractor.yaml", "cronjob-prune.yaml"):
            [job_container] = self._cronjob(name)["spec"]["jobTemplate"]["spec"]["template"][
                "spec"
            ]["containers"]
            [dsn] = [e for e in job_container["env"] if e["name"] == "TOUCHSTONE_DSN"]
            assert "optional" not in dsn["valueFrom"]["secretKeyRef"]

    def test_the_pinned_extraction_model_is_not_a_lab_tier(self) -> None:
        """Config validation rejects a -lab id at startup; catch it before the pod."""
        [container] = self._cronjob("cronjob-extractor.yaml")["spec"]["jobTemplate"]["spec"][
            "template"
        ]["spec"]["containers"]
        [model] = [
            entry["value"] for entry in container["env"]
            if entry["name"] == "TOUCHSTONE_EXTRACT_MODEL"
        ]
        assert not model.endswith("-lab")

    def test_the_destructive_job_ships_suspended(self) -> None:
        """The one manifest whose default state is the safety property."""
        spec = self._cronjob("cronjob-prune.yaml")["spec"]
        assert spec["suspend"] is True, (
            "prune deletes observations; enabling it must be a deliberate act"
        )
        [container] = spec["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        assert container["command"][:2] == ["touchstone", "prune"]
        # An explicit horizon, so a default change in code cannot silently widen it.
        assert "--days" in container["command"]
        assert "--apply" in container["command"]
        assert _secret_refs(container) == {"TOUCHSTONE_DSN": (DB_SECRET, "TOUCHSTONE_DSN")}

    def test_only_the_prune_job_is_ever_destructive(self) -> None:
        for name in ("cronjob-scanner.yaml", "cronjob-extractor.yaml"):
            [container] = self._cronjob(name)["spec"]["jobTemplate"]["spec"]["template"]["spec"][
                "containers"
            ]
            assert "prune" not in container["command"]


def test_the_namespace_pins_the_cluster_pod_security_version() -> None:
    namespace = _manifest("namespace.yaml")
    assert namespace["metadata"]["labels"] == {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "v1.35",
        "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/audit-version": "v1.35",
        "pod-security.kubernetes.io/warn": "restricted",
        "pod-security.kubernetes.io/warn-version": "v1.35",
    }


def test_no_secret_value_is_committed() -> None:
    """Manifests reference Secrets; they never carry one."""
    for name in TRACKED_MANIFESTS:
        document = _manifest(name)
        assert document["kind"] != "Secret"
        text = (K8S / name).read_text(encoding="utf-8")
        assert "stringData" not in text
        assert "\ndata:" not in text
