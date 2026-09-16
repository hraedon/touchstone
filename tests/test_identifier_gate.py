"""Tests for the publication identifier gate (``scripts/check_committed_identifiers.py``).

The gate is the mechanical guard that stops work-domain identifiers reaching a
published remote. It had no tests at all: ~550 lines whose *only* failure mode in
production is silence. A gate that stops matching still exits 0, and exit 0 is
indistinguishable from a clean tree -- so every rule below is asserted in both
directions (a tree that must fail, and a tree that must pass), never just "no
violations found".

The script is not importable as a package module (``scripts/`` is not a package),
so it is loaded from its path. That is deliberate: the tests exercise the same
file CI runs, not a copy.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_committed_identifiers.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_ad_steward_identifier_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: ``@dataclass`` resolves its own module out of
    # ``sys.modules`` to evaluate annotations, and an unregistered module makes
    # that lookup return None mid-decoration.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

# The guarded-dir set is deliberately repo-specific: it names THIS repo's
# gitignored data directories, and a repo whose charter differs (guarding
# evidence/staging/local rather than samples/) is a supported configuration, not
# a deviation. The CLI-level tests below therefore ask the module what it guards
# instead of hardcoding "samples" -- otherwise this file tests one repo's
# spelling of the rule rather than the rule, and silently passes vacuously
# wherever the spelling differs.
GUARDED = sorted(gate._GUARDED_DIRS)[0]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway git repo, with the CWD moved into it.

    The gate resolves its tree via ``git ls-files`` and its publication
    declaration via ``git rev-parse --show-toplevel``. Without a real repo the
    tests would silently read *ad-steward's own* tree and publication.toml --
    which is exactly the "aimed at the wrong target" failure the gate is meant to
    prevent, reproduced in the test suite.
    """
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    monkeypatch.chdir(root)
    return root


def _track(root: Path, relpath: str, content: str | bytes) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-f", "--", relpath], cwd=root, check=True)
    return path


def _commit(root: Path, message: str) -> str:
    subprocess.run(["git", "commit", "-q", "--no-verify", "-m", message], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _staged_blob(root: Path, relpath: str) -> str:
    """The stage-0 index content for *relpath* -- the bytes a commit would record."""
    return subprocess.run(
        ["git", "show", f":0:{relpath}"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout


def _declare(root: Path, visibility: str) -> None:
    (root / "publication.toml").write_text(
        f'[publication]\nremote_owner = "someone"\nvisibility = "{visibility}"\n',
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# parse_identifier_set
# --------------------------------------------------------------------------


def test_parses_whitespace_separated_secret_form() -> None:
    assert gate.parse_identifier_set("alpha beta gamma") == frozenset(
        {"alpha", "beta", "gamma"}
    )


def test_parses_one_entry_per_line() -> None:
    assert gate.parse_identifier_set("alpha\nbeta\n") == frozenset({"alpha", "beta"})


def test_strips_full_line_and_trailing_comments() -> None:
    raw = "# a full-line comment about servers\nalpha  # trailing note\nbeta\n"
    parsed = gate.parse_identifier_set(raw)
    assert parsed == frozenset({"alpha", "beta"})
    # The comment words must not become forbidden tokens -- otherwise documenting
    # the denylist would start failing the gate on innocent prose.
    assert "comment" not in parsed
    assert "servers" not in parsed


def test_keeps_quoted_multi_word_entry_whole() -> None:
    """The blind spot that hid a two-word name in sixteen repos.

    Unquoted, the halves are separate short tokens and the length filter drops
    them; quoted, the phrase survives as one entry.
    """
    assert gate.parse_identifier_set('"two words"') == frozenset({"two words"})
    # Unquoted, the same text is two independent tokens: the phrase cannot be
    # expressed at all, and any half below the length floor vanishes silently.
    unquoted = gate.parse_identifier_set("two words")
    assert "two words" not in unquoted
    assert unquoted == frozenset({"words"})
    # Both halves short: the entry disappears completely, matching nothing.
    assert gate.parse_identifier_set("ab cd") == frozenset()
    assert gate.parse_identifier_set('"ab cd"') == frozenset({"ab cd"})


def test_normalizes_case_and_internal_whitespace() -> None:
    assert gate.parse_identifier_set('"Two   Words"') == frozenset({"two words"})


def test_drops_tokens_below_the_minimum_length() -> None:
    short = "x" * (gate.MIN_IDENTIFIER_LENGTH - 1)
    long = "y" * gate.MIN_IDENTIFIER_LENGTH
    assert gate.parse_identifier_set(f"{short} {long}") == frozenset({long})


def test_unbalanced_quote_raises_rather_than_degrading() -> None:
    """A denylist we cannot parse must fail loudly, not silently shrink.

    Degrading to a partial token set is the dangerous outcome: the gate would run,
    report nothing, and exit 0 while scanning for fewer identifiers than declared.
    """
    with pytest.raises(ValueError):
        gate.parse_identifier_set('"unterminated')


def test_unbalanced_quote_makes_the_cli_exit_one(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _track(repo, "README.md", "hello\n")
    _commit(repo, "init")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", '"unterminated')
    assert gate.main([]) == 1


# --------------------------------------------------------------------------
# scan_text
# --------------------------------------------------------------------------


def test_match_is_case_insensitive() -> None:
    found = list(gate.scan_text("The WIDGETCORP server", frozenset({"widgetcorp"})))
    assert [v.identifier for v in found] == ["widgetcorp"]


def test_match_counts_substring_occurrences_inside_longer_tokens() -> None:
    """Real identifiers legitimately appear inside longer tokens."""
    found = list(gate.scan_text("host-widgetcorp-01.example", frozenset({"widgetcorp"})))
    assert len(found) == 1


def test_reports_every_occurrence_on_a_line() -> None:
    found = list(gate.scan_text("widgetcorp and widgetcorp", frozenset({"widgetcorp"})))
    assert len(found) == 2


def test_reports_the_correct_line_number() -> None:
    text = "clean\nclean\nwidgetcorp here\n"
    (violation,) = list(gate.scan_text(text, frozenset({"widgetcorp"})))
    assert violation.line_number == 3
    assert violation.line == "widgetcorp here"


def test_clean_text_yields_nothing() -> None:
    assert list(gate.scan_text("nothing to see\n", frozenset({"widgetcorp"}))) == []


def test_empty_identifier_set_yields_nothing() -> None:
    assert list(gate.scan_text("widgetcorp", frozenset())) == []


@pytest.mark.parametrize(
    "spelling",
    ["two words", "two-words", "two_words", "two.words", "two   words"],
)
def test_phrase_matches_every_separator_spelling(spelling: str) -> None:
    """One denylist entry must cover every way prose spells the phrase."""
    found = list(gate.scan_text(f"the {spelling} estate", frozenset({"two words"})))
    assert len(found) == 1, f"{spelling!r} escaped the phrase pattern"


def test_phrase_matches_across_a_line_break() -> None:
    """Wrapped prose is the case a line-by-line scanner cannot see."""
    text = "a sentence mentioning two\nwords in passing\n"
    (violation,) = list(gate.scan_text(text, frozenset({"two words"})))
    assert violation.line_number == 1


def test_phrase_does_not_match_across_an_unrelated_word() -> None:
    assert list(gate.scan_text("two other words", frozenset({"two words"}))) == []


def test_phrase_matching_is_case_insensitive() -> None:
    """Prose capitalises. A phrase entry must survive title case.

    Single-word matching lowercases the line; phrase matching goes through a
    separate compiled pattern, so the two can drift apart in exactly this way.
    """
    assert len(list(gate.scan_text("The Two Words estate", frozenset({"two words"})))) == 1


def test_phrase_metacharacters_are_escaped_literally() -> None:
    """A denylist entry is data, not a regex.

    ``acme (uk)`` unescaped compiles to ``acme[sep]+(uk)`` -- a capturing group
    that matches the *unrelated* string "acme uk" while missing the literal name
    it was written to catch. Both directions are asserted, because getting this
    wrong swaps which strings the gate sees rather than merely losing matches.

    Note this exercises the phrase path specifically: a single-word entry is
    matched with ``str.find`` and never reaches the regex at all.
    """
    entry = frozenset({"acme (uk)"})
    assert len(list(gate.scan_text("the acme (uk) estate", entry))) == 1
    assert list(gate.scan_text("the acme uk estate", entry)) == []


# --------------------------------------------------------------------------
# scan_files
# --------------------------------------------------------------------------


def test_scans_a_plain_utf8_file_and_records_the_path(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("widgetcorp\n", encoding="utf-8")
    (violation,) = gate.scan_files(frozenset({"widgetcorp"}), [target])
    assert violation.path == target


@pytest.mark.parametrize(
    ("encoding", "bom", "has_nulls"),
    [
        ("utf-16-le", b"\xff\xfe", True),
        ("utf-16-be", b"\xfe\xff", True),
        ("utf-8-sig", b"", False),  # the codec emits its own BOM
    ],
)
def test_bom_marked_text_is_decoded_not_dismissed_as_binary(
    tmp_path: Path, encoding: str, bom: bytes, has_nulls: bool
) -> None:
    """UTF-16 is common in Windows tooling output and is full of null bytes.

    The null-byte heuristic alone would classify it as binary and skip it --
    a whole file class silently exempt from the gate. The explicit-endian codecs
    emit no BOM of their own, so the marker is written deliberately, which is how
    the Windows tools producing these files write them.
    """
    target = tmp_path / "export.txt"
    target.write_bytes(bom + "widgetcorp\n".encode(encoding))
    raw = target.read_bytes()
    assert gate._sniff_encoding(raw) == encoding
    assert (b"\x00" in raw) is has_nulls
    assert gate._is_binary(raw) is False, "a BOM must override the null-byte heuristic"
    assert len(gate.scan_files(frozenset({"widgetcorp"}), [target])) == 1


def test_utf16_bom_does_not_leak_into_the_reported_line(tmp_path: Path) -> None:
    """An explicit-endian UTF-16 decode leaves U+FEFF at the start of line 1.

    It hides nothing -- matching is substring-based, so the identifier is found
    either way -- but a report that prints an invisible character before the
    offending text is one people mistrust, and the two scan modes must agree.
    """
    target = tmp_path / "export.txt"
    target.write_bytes(b"\xff\xfe" + "widgetcorp is here\n".encode("utf-16-le"))
    (violation,) = gate.scan_files(frozenset({"widgetcorp"}), [target])
    assert violation.line == "widgetcorp is here"
    assert not violation.line.startswith("\ufeff")


def test_staged_utf16_bom_is_stripped_too(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staged path must strip it as well, or the modes disagree."""
    _track(repo, "export.txt", b"\xff\xfe" + "widgetcorp is here\n".encode("utf-16-le"))
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    violations = gate.scan_staged_blobs(
        frozenset({"widgetcorp"}), [Path("export.txt")]
    )
    assert violations and violations[0].line == "widgetcorp is here"


def test_genuine_binary_is_skipped(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01\x02widgetcorp")
    assert gate.scan_files(frozenset({"widgetcorp"}), [target]) == []


def test_symlink_target_string_is_scanned_without_following_the_link(
    tmp_path: Path,
) -> None:
    """A tracked symlink's blob content IS its target path.

    Following it either leaves the repo or fails on a broken link; the target
    string itself can carry the identifier, so it is scanned in place.
    """
    link = tmp_path / "link"
    link.symlink_to("/srv/widgetcorp/data")
    (violation,) = gate.scan_files(frozenset({"widgetcorp"}), [link])
    assert violation.path == link
    assert violation.line == "/srv/widgetcorp/data"


def test_broken_symlink_does_not_count_as_unreadable(tmp_path: Path) -> None:
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "does-not-exist")
    unreadable: list[Path] = []
    assert gate.scan_files(frozenset({"widgetcorp"}), [link], unreadable=unreadable) == []
    assert unreadable == []


def test_unreadable_file_is_collected_rather_than_silently_skipped(
    tmp_path: Path,
) -> None:
    """Skipping an unreadable file is the fails-open case the gate exists to stop."""
    target = tmp_path / "secret.md"
    target.write_text("widgetcorp\n", encoding="utf-8")
    target.chmod(0o000)
    try:
        if os.access(target, os.R_OK):  # root ignores the mode bits
            pytest.skip("cannot make a file unreadable as this user")
        unreadable: list[Path] = []
        violations = gate.scan_files(frozenset({"widgetcorp"}), [target], unreadable=unreadable)
        assert violations == []
        assert unreadable == [target]
    finally:
        target.chmod(0o644)


def test_scan_files_returns_a_list_not_a_tuple(tmp_path: Path) -> None:
    """Fleet-wide contract: this script is copied into every repo in the estate
    and several of them assert on ``scan_files``' return type directly. Returning
    a tuple once broke seven test suites at the same time.
    """
    assert isinstance(gate.scan_files(frozenset({"widgetcorp"}), []), list)


# --------------------------------------------------------------------------
# leaked_tracked_files (the always-on guard)
# --------------------------------------------------------------------------


def test_root_level_samples_file_is_flagged() -> None:
    leaked = gate.leaked_tracked_files([Path("samples/capture.json")], frozenset({"samples"}))
    assert leaked == [Path("samples/capture.json")]


def test_nested_samples_directory_is_not_a_false_positive() -> None:
    """``tests/samples/`` is a legitimate code directory, not the data dir."""
    nested = [Path("tests/samples/fixture.json")]
    assert gate.leaked_tracked_files(nested, frozenset({"samples"})) == []


@pytest.mark.parametrize(
    "name", ["notes.swp", "notes.swo", ".notes.md.swp", ".notes.md.swn"]
)
def test_editor_swap_files_are_never_tracked(name: str) -> None:
    """A swap file holds the BUFFER of the file being edited.

    A secret typed and not yet saved lives in there, so it is guarded regardless
    of denylist configuration. Vim's collision sequence (.swo, .swn, ... once
    .swp is taken) is why suffix matching alone is not enough.
    """
    assert gate.leaked_tracked_files([Path(name)], frozenset()) == [Path(name)]


def test_a_swap_file_deep_in_the_tree_is_still_caught() -> None:
    p = Path("src/deep/.thing.py.swp")
    assert gate.leaked_tracked_files([p], frozenset()) == [p]


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production"])
def test_root_level_env_files_are_never_tracked(name: str) -> None:
    assert gate.leaked_tracked_files([Path(name)], frozenset()) == [Path(name)]


def test_env_example_is_the_deliberately_tracked_template() -> None:
    assert gate.leaked_tracked_files([Path(".env.example")], frozenset()) == []


def test_a_nested_env_file_is_not_guarded() -> None:
    """Scoped to the ROOT, so a fixture like tests/fixtures/.env.broken stays possible."""
    assert gate.leaked_tracked_files([Path("tests/fixtures/.env.broken")], frozenset()) == []


def test_ordinary_dotfiles_are_not_guarded() -> None:
    """The rules must not swallow normal repo furniture."""
    ordinary = [Path(".gitignore"), Path(".editorconfig"), Path("env.py"), Path("a.swap")]
    assert gate.leaked_tracked_files(ordinary, frozenset()) == []


def test_guard_fires_through_the_cli_on_a_force_added_sample(repo: Path) -> None:
    """``.gitignore`` is advisory -- ``git add -f`` bypasses it. This is the catch."""
    _track(repo, f"{GUARDED}/capture.json", "{}\n")
    _commit(repo, "force-add a capture")
    assert gate.main([]) == 1


def test_cli_passes_a_tree_with_no_guarded_files(repo: Path) -> None:
    _track(repo, f"tests/{GUARDED}/fixture.json", "{}\n")
    _commit(repo, "add a legitimate nested fixture")
    assert gate.main([]) == 0


# --------------------------------------------------------------------------
# Unconfigured-denylist semantics: the silent-pass asymmetry
# --------------------------------------------------------------------------


def test_unset_secret_is_a_no_op_without_a_declaration(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo that never opted into the publication system is not blocked."""
    _track(repo, "README.md", "hello\n")
    _commit(repo, "init")
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert not (repo / "publication.toml").exists()
    assert gate.main([]) == 0


def test_unset_secret_is_a_no_op_for_a_private_repo(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(repo, "private-until-review")
    _track(repo, "publication.toml", (repo / "publication.toml").read_text())
    _commit(repo, "declare private")
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main([]) == 0


def test_unset_secret_fails_closed_for_a_public_repo(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point. On a public repo "skipping" and "clean" look identical,
    and a leak there is irreversible -- so an unconfigured gate must fail.
    """
    _declare(repo, "public")
    _track(repo, "publication.toml", (repo / "publication.toml").read_text())
    _commit(repo, "declare public")
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main([]) == 1
    assert "silent pass" in capsys.readouterr().err


def test_public_repo_with_a_configured_gate_and_clean_tree_passes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(repo, "public")
    _track(repo, "publication.toml", (repo / "publication.toml").read_text())
    _track(repo, "README.md", "nothing sensitive here\n")
    _commit(repo, "declare public")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    assert gate.main([]) == 0


def test_all_short_denylist_entries_fail_closed_for_a_public_repo(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret that parses to nothing is unconfigured by another name."""
    _declare(repo, "public")
    _track(repo, "publication.toml", (repo / "publication.toml").read_text())
    _commit(repo, "declare public")
    too_short = "x" * (gate.MIN_IDENTIFIER_LENGTH - 1)
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", too_short)
    assert gate.main([]) == 1


def test_unparseable_declaration_fails_rather_than_guessing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Present-but-broken is not the same as absent.

    That repo *did* opt in, and guessing its visibility is exactly the coin-flip
    the declaration exists to remove.
    """
    (repo / "publication.toml").write_text("[publication\nnot = toml", encoding="utf-8")
    _track(repo, "README.md", "hello\n")
    _commit(repo, "init")
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main([]) == 1


def test_declaration_without_a_publication_table_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "publication.toml").write_text('[other]\nkey = "value"\n', encoding="utf-8")
    _track(repo, "README.md", "hello\n")
    _commit(repo, "init")
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main([]) == 1


# --- Visibility normalisation -------------------------------------------------
#
# These pin the declaration surface in BOTH directions. Before them, three
# mutations survived the suite: dropping .strip(), treating a missing key as
# public, and accepting uppercase. All three change whether a public repo's gate
# arms at all, and none of them made a test go red -- so the fail-open behaviour
# they describe was not just wrong, nothing would have noticed it being fixed or
# worsened. A declaration shape that is not exactly the lowercase word is the
# realistic case: the publication-review commit that flips visibility to public
# is precisely where a case typo or a dropped line lands.


@pytest.mark.parametrize("spelling", ["Public", "PUBLIC", "  public  ", "PuBlIc"])
def test_public_is_recognised_whatever_its_case_or_padding(
    repo: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """Any casing of "public" must still arm the gate.

    Recognising only the exact lowercase string sent every other spelling to the
    fail-OPEN branch, which silently disarmed the gate on a public repo.
    """
    (repo / "publication.toml").write_text(
        f'[publication]\nremote_owner = "someone"\nvisibility = "{spelling}"\n',
        encoding="utf-8",
    )
    _track(repo, "README.md", "hello\n")
    _commit(repo, "declare public")
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main([]) == 1


@pytest.mark.parametrize(
    "body",
    [
        '[publication]\nremote_owner = "someone"\n',  # visibility key absent
        '[publication]\nvisibility = ""\n',  # empty string
        "[publication]\nvisibility = true\n",  # not a string
        "[publication]\nvisibility = 1\n",  # not a string
        '[publication]\nvisibility = "publik"\n',  # typo
        '[publication]\nvisibility = "internal"\n',  # not in the closed set
    ],
)
def test_unrecognised_visibility_fails_rather_than_guessing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """A declaration that opted in but names an unknown visibility is an error.

    Quietly reading it as "not public" is the coin-flip the declaration exists to
    remove, and it resolves in the unsafe direction: the gate no-ops and CI is
    green having scanned nothing.
    """
    (repo / "publication.toml").write_text(body, encoding="utf-8")
    _track(repo, "README.md", "hello\n")
    _commit(repo, "declare")
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main([]) == 1


@pytest.mark.parametrize(
    "spelling", ["PRIVATE-UNTIL-REVIEW", "Private-Until-Review", "  private-until-review  "]
)
def test_private_until_review_is_recognised_whatever_its_case_or_padding(
    repo: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """The normalisation must work on the private side too, not only the public one.

    Without this, dropping .casefold() or .strip() looks harmless: every public
    spelling still exits 1, just as a GateError instead of a recognised public.
    The mutation only shows up here, where over-strictness turns a repo that must
    stay clonable into a hard block.
    """
    (repo / "publication.toml").write_text(
        f'[publication]\nremote_owner = "someone"\nvisibility = "{spelling}"\n',
        encoding="utf-8",
    )
    _track(repo, "README.md", "hello\n")
    _commit(repo, "declare private")
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main([]) == 0


def test_oddly_cased_public_is_scanned_rather_than_merely_erroring(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recognised public repo with a working denylist scans and passes clean.

    This separates "recognised as public, scanned, nothing found" from "could not
    read the declaration at all". Both exit 1 when the denylist is missing, so the
    public-side tests alone cannot tell a working normalisation from a broken one.
    """
    (repo / "publication.toml").write_text(
        '[publication]\nremote_owner = "someone"\nvisibility = "Public"\n',
        encoding="utf-8",
    )
    _track(repo, "README.md", "nothing forbidden here\n")
    _commit(repo, "declare public")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "zzzsynthetictoken")
    assert gate.main([]) == 0


def test_private_until_review_still_no_ops_without_a_denylist(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: tightening public must not block a private repo.

    Without this, a fix that made everything fail closed would look correct.
    """
    _declare(repo, "private-until-review")
    _track(repo, "publication.toml", (repo / "publication.toml").read_text())
    _commit(repo, "declare private")
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main([]) == 0


def test_configured_gate_catches_an_identifier_in_a_tracked_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _track(repo, "docs/notes.md", "the widgetcorp estate\n")
    _commit(repo, "add notes")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    assert gate.main([]) == 1


def test_configured_gate_catches_a_quoted_phrase_in_a_tracked_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _track(repo, "docs/notes.md", "the two-words estate\n")
    _commit(repo, "add notes")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", '"two words"')
    assert gate.main([]) == 1


def test_unreadable_tracked_file_blocks_the_cli(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _track(repo, "docs/locked.md", "clean content\n")
    _commit(repo, "add a file")
    target.chmod(0o000)
    try:
        if os.access(target, os.R_OK):
            pytest.skip("cannot make a file unreadable as this user")
        monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
        assert gate.main([]) == 1
    finally:
        target.chmod(0o644)


# --------------------------------------------------------------------------
# Commit-message modes (the channel the content scan cannot see)
# --------------------------------------------------------------------------


def test_commit_message_file_with_an_identifier_is_rejected(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("redact widgetcorp from the docs\n", encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    assert gate.main(["--message-file", str(msg)]) == 1


def test_clean_commit_message_file_passes(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("redact the customer name from the docs\n", encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    assert gate.main(["--message-file", str(msg)]) == 0


def test_commit_message_comment_lines_are_ignored(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git strips ``#`` lines, so they are never published and must not block."""
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("a clean subject\n#\n# On branch widgetcorp-fix\n", encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    assert gate.main(["--message-file", str(msg)]) == 0


def test_rev_range_scans_published_commit_messages(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented leak: the very commits that redacted an identifier from the
    files named it in their messages, and the tracked-tree scan cannot see that.
    """
    _track(repo, "README.md", "clean\n")
    base = _commit(repo, "a clean base commit")
    _track(repo, "README.md", "still clean\n")
    _commit(repo, "remove widgetcorp from the docs")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    assert gate.main(["--rev-range", f"{base}..HEAD"]) == 1


def test_rev_range_passes_when_every_message_is_clean(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _track(repo, "README.md", "clean\n")
    base = _commit(repo, "a clean base commit")
    _track(repo, "README.md", "still clean\n")
    _commit(repo, "tidy the documentation")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    assert gate.main(["--rev-range", f"{base}..HEAD"]) == 0


def test_rev_range_accepts_the_multi_argument_new_branch_form(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """pre-push passes ``"<sha> --not --remotes=<name>"`` as one string for a
    branch with no upstream; it must be split, not treated as one opaque ref.

    Asserting only "exit 1" would be vacuous here: passing the whole string as a
    single ref makes *git itself* fail, which also exits 1. So the clean case must
    exit 0 (git ran and found nothing), and the dirty case must name a forbidden
    identifier rather than report that the gate could not complete.
    """
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")

    _track(repo, "README.md", "clean\n")
    _commit(repo, "a perfectly clean subject")
    assert gate.main(["--rev-range", "HEAD --not --remotes=origin"]) == 0
    assert "could not complete" not in capsys.readouterr().err

    _track(repo, "README.md", "still clean\n")
    _commit(repo, "mentions widgetcorp in the message")
    assert gate.main(["--rev-range", "HEAD --not --remotes=origin"]) == 1
    err = capsys.readouterr().err
    assert "Forbidden identifier in commit message" in err
    assert "could not complete" not in err


# --------------------------------------------------------------------------
# Staged mode (pre-commit hook)
# --------------------------------------------------------------------------


def test_staged_mode_scans_only_what_is_about_to_be_committed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _track(repo, "docs/old.md", "the widgetcorp estate\n")
    _commit(repo, "a pre-existing file")
    _track(repo, "docs/new.md", "perfectly clean\n")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    # The committed file is dirty, but it is not staged: --staged must not fail.
    assert gate.main(["--staged"]) == 0
    # The whole-tree scan, which CI runs, still sees it.
    assert gate.main([]) == 1


def test_staged_mode_catches_a_rename_into_a_guarded_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-renames`` decomposes a rename into add+delete so the NEW path is
    visible to the always-on guard. Without it a file moved into ``samples/``
    would be reported only as a rename and slip past.
    """
    _track(repo, "capture.json", '{"host": "x"}\n')
    _commit(repo, "add a capture at the root")
    subprocess.run(["git", "mv", "capture.json", "moved.json"], cwd=repo, check=True)
    (repo / GUARDED).mkdir()
    dest = f"{GUARDED}/capture.json"
    subprocess.run(["git", "mv", "moved.json", dest], cwd=repo, check=True)
    subprocess.run(["git", "add", "-f", "--", dest], cwd=repo, check=True)
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main(["--staged"]) == 1


def test_staged_mode_judges_the_index_not_the_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bypass this scanner exists to close.

    A commit records the INDEX. Staging a forbidden identifier and then
    overwriting the working copy with clean bytes leaves an index blob that
    still carries it -- and a gate that reads the worktree sees only the clean
    bytes, passes, and lets the forbidden blob into history. The worktree
    content here is deliberately innocent: if this test ever passes by reading
    the file, it is reading the wrong thing.
    """
    _track(repo, "notes.md", "the widgetcorp estate\n")
    (repo / "notes.md").write_text("perfectly innocent text\n", encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")

    assert (repo / "notes.md").read_text() == "perfectly innocent text\n"
    assert "widgetcorp" in _staged_blob(repo, "notes.md")
    assert gate.main(["--staged"]) == 1


def test_staged_mode_ignores_unstaged_worktree_content(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse, which matters just as much.

    A clean index under a dirty worktree was blocked for content no commit was
    going to record. A gate that cries wolf on work in progress trains people to
    reach for --no-verify, which disables it entirely.
    """
    _track(repo, "notes.md", "perfectly innocent text\n")
    (repo / "notes.md").write_text("the widgetcorp estate\n", encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")

    assert gate.main(["--staged"]) == 0

    # The two modes read different things, and that is the whole point. The
    # default scan reads the CHECKED-OUT bytes, so it still sees the dirty
    # worktree and refuses. In CI the distinction is invisible because the
    # checkout is pristine and index, worktree and HEAD all agree -- which is
    # exactly why this divergence has to be pinned by a test rather than noticed.
    _commit(repo, "commit the clean index")
    assert gate.main([]) == 1


def test_staged_binary_blob_is_skipped(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary handling must match scan_files, or the two modes disagree."""
    _track(repo, "blob.bin", b"\x00\x01\x02widgetcorp")
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    assert gate.main(["--staged"]) == 0


def test_staged_utf16_blob_is_decoded(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _track(repo, "export.txt", b"\xff\xfe" + "widgetcorp\n".encode("utf-16-le"))
    monkeypatch.setenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", "widgetcorp")
    assert gate.main(["--staged"]) == 1


def test_scan_staged_blobs_returns_a_list(repo: Path) -> None:
    """Same fleet-wide return-type contract as scan_files."""
    assert isinstance(gate.scan_staged_blobs(frozenset({"widgetcorp"}), []), list)


# --------------------------------------------------------------------------
# Failing clean: a gate that cannot judge must not look like a pass
# --------------------------------------------------------------------------


def test_git_failure_becomes_a_clean_exit_one_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Outside a repo, ``git ls-files`` fails. A publication gate must report that
    as a blocked publication, not a CalledProcessError stack trace that reads as
    broken infrastructure.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOUCHSTONE_FORBIDDEN_IDENTIFIERS", raising=False)
    assert gate.main([]) == 1
    assert "identifier gate could not complete" in capsys.readouterr().err


def test_run_git_raises_gate_error_when_git_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("no git here")

    monkeypatch.setattr(gate.subprocess, "run", _boom)
    with pytest.raises(gate.GateError):
        gate._run_git(["git", "status"])


def test_gate_error_message_names_the_failing_command() -> None:
    with pytest.raises(gate.GateError) as excinfo:
        gate._run_git([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert "exit 3" in str(excinfo.value)
