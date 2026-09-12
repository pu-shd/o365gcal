"""Keeps the documentation honest.

SYNCHRONIZATION.md cites specific tests and specific default values as evidence. A
document that names a test which does not exist, or quotes a default that has since
changed, is worse than no document: it is confidently wrong, and a reader has no way
to tell.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SYNC = DOCS / "SYNCHRONIZATION.md"


def all_test_names() -> set[str]:
    names = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        names |= set(re.findall(r"^def (test_\w+)", path.read_text(), re.M))
    return names


def all_test_files() -> set[str]:
    return {
        str(p.relative_to(ROOT)) for p in (ROOT / "tests").rglob("test_*.py")
    }


def test_sync_doc_exists():
    assert SYNC.exists(), "the synchronization reference is the main behavioural doc"


def test_every_cited_test_exists():
    """Cited as `test_name` in backticks after a 'Held by' or arrow marker."""
    text = SYNC.read_text()
    cited = set(re.findall(r"`(test_\w+)`", text))
    assert cited, "the doc should cite the tests that hold its claims"
    missing = cited - all_test_names()
    assert not missing, f"SYNCHRONIZATION.md cites tests that do not exist: {sorted(missing)}"


def test_every_cited_test_file_exists():
    text = SYNC.read_text()
    cited = set(re.findall(r"`(tests/[\w/]+\.py)`", text))
    missing = cited - all_test_files()
    assert not missing, f"cites test files that do not exist: {sorted(missing)}"


#: Numbers the document states as fact, and where each really comes from.
DOCUMENTED_DEFAULTS = {
    "max_mutations_per_run": 60,
    "max_verify_per_run": 10,
    "verify_slices": 16,
    "max_delete_percent": 25,
    "min_deletes_before_breaker": 5,
    "window_past_days": 7,
    "window_future_days": 120,
    "map_retention_days": 400,
    "deleted_row_retention_days": 30,
    "list_size_warn_at": 4000,
}


@pytest.mark.parametrize("field,value", sorted(DOCUMENTED_DEFAULTS.items()))
def test_documented_defaults_match_the_code(field, value):
    """If a default changes, this fails and the doc gets corrected with it."""
    from o365gcal.model import Config

    assert getattr(Config(), field) == value, (
        f"Config.{field} is {getattr(Config(), field)}, but the documentation and this "
        f"test both say {value}. Update all three together."
    )


def test_documented_budget_is_arithmetically_true():
    """The doc's central claim about fitting inside the connector limit must add up."""
    from o365gcal.model import Config

    c = Config()
    assert c.max_mutations_per_run + c.max_verify_per_run == 70
    text = SYNC.read_text()
    assert "≤ 70 of 100" in text


def test_the_retry_caveat_is_stated():
    """The 70-call figure holds only when nothing retries. With four retries per action
    a 60-mutation cap can reach 300 calls, and a document that omitted that would be
    reassuring and wrong."""
    text = SYNC.read_text()
    assert "counts *attempts*, not API calls" in text
    assert "300 calls" in text
    assert "stopped early" in text.lower()


def test_documented_sweep_length_is_arithmetically_true():
    """16 slices at a 15-minute cadence is claimed as a four-hour sweep."""
    from o365gcal.model import Config

    assert Config().verify_slices * 15 == 240
    assert "every 4 hours" in SYNC.read_text()


def test_the_google_limit_is_attributed_correctly():
    """The single most consequential fact in the document: the limit is Microsoft's,
    so paying Google more does not raise it."""
    text = SYNC.read_text()
    assert "100 calls per 60 seconds" in text
    assert "Microsoft's, not Google's" in text


def test_readme_links_to_the_sync_reference():
    assert "SYNCHRONIZATION.md" in (ROOT / "README.md").read_text()


@pytest.mark.parametrize("doc", ["INSTALL.md", "ADMIN.md", "ARCHITECTURE.md",
                                 "TROUBLESHOOTING.md", "SYNCHRONIZATION.md"])
def test_docs_do_not_quote_a_stale_cadence(doc):
    """The reconciler moved from 30 to 15 minutes; a doc still saying 30 would send
    someone looking for a fault that is not there."""
    text = (DOCS / doc).read_text()
    for stale in ("every 30 minutes", "30-minute cycle", "within 30 minutes"):
        assert stale not in text, f"{doc} still says '{stale}'"


# --------------------------------------------------------------- prerequisites

#: The scripts a person following INSTALL.md actually runs. A tool that one of these
#: refuses to start without is a prerequisite, and a prerequisite the documentation
#: does not name is a dead end: the very first command in both README.md and
#: INSTALL.md is `pac auth create`, which fails with "command not found" *before*
#: install.sh gets the chance to explain how to install pac.
USER_SCRIPTS = [
    "install.sh", "bootstrap.sh", "configure.sh", "status.sh", "update.sh",
    "teardown.sh", "enable-flows.sh", "backup.sh", "restore.sh", "run-flow.sh",
    "preflight.sh", "show-state.sh",
]

#: Every hard requirement, and the string that proves INSTALL.md says how to get it.
#: Documenting the name alone is not enough — "you need pac" without a command is
#: the same dead end one sentence later.
INSTALL_EVIDENCE = {
    "pac": "Microsoft.PowerApps.CLI.Tool",
    "az": "azure-cli",
}


def required_tools() -> set[str]:
    """Tools a user-facing script exits over, read out of the scripts themselves.

    Matches the two forms that gate a failure — `if ! command -v x` and
    `command -v x ... ||` — and deliberately not `if command -v x`, which is how
    build.sh treats python3 as an optional nicety.
    """
    tools: set[str] = set()
    for name in USER_SCRIPTS:
        path = ROOT / "scripts" / name
        assert path.exists(), f"USER_SCRIPTS names {name}, which no longer exists"
        text = path.read_text()
        tools |= set(re.findall(r"if !\s*command -v ([a-z0-9_-]+)", text))
        tools |= set(re.findall(r"command -v ([a-z0-9_-]+)[^\n|]*\|\|", text))
    return tools


def test_the_prerequisite_scan_still_finds_something():
    """Silence must not read as success. If the scripts change how they probe for a
    tool, this fails here rather than letting every check below pass vacuously."""
    found = required_tools()
    assert {"pac", "az"} <= found, (
        f"expected pac and az to be detected as hard requirements, found {sorted(found)}"
    )


@pytest.mark.parametrize("tool", sorted(INSTALL_EVIDENCE))
def test_install_doc_says_how_to_install_each_tool(tool):
    text = (DOCS / "INSTALL.md").read_text()
    assert f"`{tool}`" in text, f"INSTALL.md never names the {tool} prerequisite"
    assert INSTALL_EVIDENCE[tool] in text, (
        f"INSTALL.md names {tool} but not how to install it "
        f"(expected to find '{INSTALL_EVIDENCE[tool]}')"
    )


def test_every_required_tool_is_documented():
    """A newly added prerequisite fails here until someone writes it down."""
    undocumented = required_tools() - set(INSTALL_EVIDENCE)
    assert not undocumented, (
        f"scripts now require {sorted(undocumented)}; add each to INSTALL.md's "
        f"'Before you start' and to INSTALL_EVIDENCE"
    )


@pytest.mark.parametrize("doc", ["README.md", "docs/INSTALL.md"])
def test_pac_is_installable_before_it_is_invoked(doc):
    """Both documents open with `pac auth create`. The install command has to come
    first on the page, or the reader hits command-not-found with no way forward."""
    text = (ROOT / doc).read_text()
    invoked = text.find("pac auth create")
    installed = text.find("Microsoft.PowerApps.CLI.Tool")
    assert invoked != -1, f"{doc} no longer mentions `pac auth create`; retarget this test"
    assert installed != -1, f"{doc} never says how to install pac"
    assert installed < invoked, (
        f"{doc} tells the reader to run `pac auth create` before saying how to install pac"
    )
