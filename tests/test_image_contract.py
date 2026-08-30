"""Build/publish guardrails that can fail before a cluster is involved."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_image_is_non_root_and_contains_the_migration_runtime() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.13-slim" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert "COPY alembic.ini ./alembic.ini" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert '"touchstone.sink.app:app"' in dockerfile
    assert '"--port", "8080"' in dockerfile


def test_build_context_excludes_secrets_and_observed_data() -> None:
    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {".env", ".env.*", "deploy/k8s/secret-*.yaml", "samples/", "data/"} <= ignored


def test_ci_gates_image_publication_and_smokes_the_hardened_container() -> None:
    assert not (ROOT / ".github" / "workflows" / "image.yml").exists()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "needs: test" in workflow
    assert "type=sha,format=long" in workflow
    assert "type=ref,event=branch" in workflow
    assert "type=semver,pattern={{version}}" in workflow
    assert "ghcr.io/hraedon/touchstone" in workflow
    assert "--read-only" in workflow
    assert "--cap-drop=ALL" in workflow
    assert "--security-opt=no-new-privileges" in workflow
    assert "docker/build-push-action@v7" in workflow
