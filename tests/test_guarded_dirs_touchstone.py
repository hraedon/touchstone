"""The publication gate must cover every repository-local secret/data escape hatch."""

from pathlib import Path

from scripts.check_committed_identifiers import _GUARDED_DIRS, leaked_tracked_files


def test_force_added_runtime_data_secrets_and_vim_swap_files_are_refused() -> None:
    candidates = [
        Path("samples/live-response.json"),
        Path("data/touchstone.db"),
        Path(".env"),
        Path(".env.production"),
        Path("..env.swp"),
        Path("src/touchstone/.app.py.swo"),
        Path("src/touchstone/.app.py.swn"),
        Path("src/touchstone/.app.py.saa"),
        Path("deploy/k8s/secret-sink.yaml"),
        Path("tests/data/synthetic-fixture.json"),
        Path(".env.example"),
        Path("assets/icon.svg"),
        Path("deploy/k8s/deployment.yaml"),
    ]

    assert set(leaked_tracked_files(candidates, _GUARDED_DIRS)) == {
        Path("samples/live-response.json"),
        Path("data/touchstone.db"),
        Path(".env"),
        Path(".env.production"),
        Path("..env.swp"),
        Path("src/touchstone/.app.py.swo"),
        Path("src/touchstone/.app.py.swn"),
        Path("src/touchstone/.app.py.saa"),
        Path("deploy/k8s/secret-sink.yaml"),
    }
