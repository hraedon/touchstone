"""Architecture invariants.

The core must stay runnable with no web framework and no model provider configured.
An import-direction violation is easy to introduce and invisible until deployment,
so it is checked mechanically.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "touchstone"

# The truth path. Nothing here may reach for the web layer or a model provider.
CORE_PACKAGES = ("scan", "ebay", "db")

FORBIDDEN_IN_CORE = ("fastapi", "jinja2", "starlette", "uvicorn", "touchstone.web")


def imports_of(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def core_files() -> list[pathlib.Path]:
    return [p for pkg in CORE_PACKAGES for p in (SRC / pkg).rglob("*.py")]


def test_core_files_exist() -> None:
    """Guards the guard: a typo'd path would make every assertion below vacuous."""
    assert len(core_files()) >= 5


def test_core_does_not_import_the_web_layer() -> None:
    violations: list[str] = []
    for path in core_files():
        for name in imports_of(path):
            if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_IN_CORE):
                violations.append(f"{path.relative_to(SRC)} imports {name}")
    assert violations == [], "\n".join(violations)


# extract.normalize / .specs / .cohort are pure string and arithmetic handling with
# no network. extract.llm is the model provider client and is the actual boundary.
PURE_EXTRACT = ("normalize", "specs", "cohort")


def test_truth_path_does_not_import_the_model_provider() -> None:
    """A scan must complete whether or not the model provider is reachable.

    scan/ may use the deterministic extract helpers; it must never import the
    provider client, because that is what would let a model outage stall a scan.
    """
    violations: list[str] = []
    for path in (SRC / "scan").rglob("*.py"):
        for name in imports_of(path):
            if name.startswith("touchstone.extract"):
                leaf = name.rsplit(".", 1)[-1]
                if leaf not in PURE_EXTRACT:
                    violations.append(f"{path.relative_to(SRC)} imports {name}")
    assert violations == [], "\n".join(violations)


def test_deterministic_extract_helpers_do_no_network() -> None:
    """Guards the boundary above: those modules are only safe for the truth path
    while they stay offline."""
    violations: list[str] = []
    for leaf in PURE_EXTRACT:
        path = SRC / "extract" / f"{leaf}.py"
        for name in imports_of(path):
            if name.split(".")[0] in {"httpx", "requests", "urllib", "socket", "http"}:
                violations.append(f"extract/{leaf}.py imports {name}")
    assert violations == [], "\n".join(violations)


def test_scan_path_imports_httpx_not_urllib() -> None:
    """A urllib TLS fingerprint draws a Cloudflare 1010 indistinguishable from the
    provider being down. Probe with the library the app uses."""
    offenders: list[str] = []
    for path in (SRC / "ebay").rglob("*.py"):
        for name in imports_of(path):
            if name.split(".")[0] == "urllib" and not name.startswith("urllib.parse"):
                offenders.append(f"{path.relative_to(SRC)} imports {name}")
    assert offenders == [], "\n".join(offenders)
