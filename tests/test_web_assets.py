"""The UI's static assets and the vendored design tokens.

These break in a way that no unit test notices: the pages render, the CSS 404s, and
the result is an unstyled page that still passes every behavioural assertion. So the
references are checked against the filesystem.
"""

from __future__ import annotations

import re
from pathlib import Path

from touchstone.web.app import STATIC_DIR, TEMPLATES_DIR

STATIC_REF = re.compile(r"url_for\(\s*'static'\s*,\s*path\s*=\s*'([^']+)'\s*\)")


def test_every_referenced_static_asset_exists() -> None:
    referenced: set[str] = set()
    for template in TEMPLATES_DIR.glob("*.html"):
        referenced.update(STATIC_REF.findall(template.read_text(encoding="utf-8")))
    assert referenced, "no static references found — the matcher has drifted"

    missing = [path for path in sorted(referenced) if not (STATIC_DIR / path).is_file()]
    assert missing == [], f"referenced but not present: {missing}"


def test_the_tokens_file_is_the_vendored_patina_contract() -> None:
    """The coherence contract across the tool family is the token *names*.

    A hand-rolled stylesheet here would look identical on the day it was written and
    drift from every sibling tool thereafter.
    """
    tokens = (STATIC_DIR / "css" / "tokens.css").read_text(encoding="utf-8")
    assert "VENDORED FROM patina" in tokens
    assert "accent: touchstone" in tokens
    for name in ("--bg", "--panel", "--border", "--text", "--ok", "--warn", "--crit", "--accent"):
        assert f"{name}:" in tokens, f"{name} missing from the vendored tokens"


def test_the_accent_is_not_a_status_colour_and_not_a_sibling_tool_s() -> None:
    """touchstone's pages are mostly coloured figures.

    An accent that collided with --ok would read as "this number is fine", and one
    that collided with gpo-lens's verdigris would make two tools indistinguishable.
    """
    tokens = (STATIC_DIR / "css" / "tokens.css").read_text(encoding="utf-8")
    accents = re.findall(r"--accent:\s*(#[0-9a-fA-F]{6})", tokens)
    assert accents, "no accent defined"
    taken = {"#c9a25a", "#5fb3a3", "#5fcde4", "#b07cc6", "#34d399", "#fbbf24", "#f87171"}
    assert not (set(accent.lower() for accent in accents) & taken)


def test_the_fonts_are_self_hosted_with_their_licence() -> None:
    fonts = STATIC_DIR / "fonts"
    assert list(fonts.glob("*.woff2")), "IBM Plex Mono must be self-hosted; there is no CDN"
    assert (fonts / "LICENSE-IBM-Plex-Mono.txt").is_file()


def test_no_template_reaches_outside_the_origin() -> None:
    """The CSP forbids it; this says so before a page 500s in a browser console."""
    offenders: list[str] = []
    for template in Path(TEMPLATES_DIR).glob("*.html"):
        body = template.read_text(encoding="utf-8")
        for match in re.findall(r'(?:src|href)="(https?://[^"]+)"', body):
            offenders.append(f"{template.name} -> {match}")
    assert offenders == [], "\n".join(offenders)
