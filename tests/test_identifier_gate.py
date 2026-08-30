"""The publication gate must cover every repository-local secret/data escape hatch."""

from pathlib import Path

from scripts.check_committed_identifiers import _GUARDED_DIRS, leaked_tracked_files


def test_force_added_runtime_data_and_operator_secrets_are_refused() -> None:
    candidates = [
        Path("samples/live-response.json"),
        Path("data/touchstone.db"),
        Path(".env"),
        Path(".env.production"),
        Path("deploy/k8s/secret-sink.yaml"),
        Path("tests/data/synthetic-fixture.json"),
        Path(".env.example"),
        Path("deploy/k8s/deployment.yaml"),
    ]

    assert set(leaked_tracked_files(candidates, _GUARDED_DIRS)) == {
        Path("samples/live-response.json"),
        Path("data/touchstone.db"),
        Path(".env"),
        Path(".env.production"),
        Path("deploy/k8s/secret-sink.yaml"),
    }
