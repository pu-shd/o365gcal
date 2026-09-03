"""Checks on the lifecycle scripts.

These are the interface a person actually touches, and a broken one is worse than a
broken flow: it fails at the moment someone is trying to install or, worse, remove
things.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((ROOT / "scripts").glob("*.sh"))
LIFECYCLE = ["bootstrap.sh", "status.sh", "update.sh", "teardown.sh",
             "build.sh", "export.sh", "preflight.sh", "fetch-connector-swagger.sh"]


def test_all_lifecycle_scripts_present():
    names = {p.name for p in SCRIPTS}
    assert set(LIFECYCLE) <= names, f"missing: {sorted(set(LIFECYCLE) - names)}"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_executable(path):
    assert os.access(path, os.X_OK), f"{path.name} is not executable"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_zsh_syntax(path):
    """CLAUDE.md targets macOS first, so these are zsh and must parse as zsh."""
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh not available")
    r = subprocess.run([zsh, "-n", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, f"{path.name}: {r.stderr}"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_uses_strict_mode(path):
    """Without `set -e`, a failed pac call carries on to the next step and reports
    success - the worst possible behaviour in an install or teardown script."""
    text = path.read_text()
    sets_directly = "set -euo pipefail" in text
    inherits = re.search(r'source\s+"\$\{0:A:h\}/common\.sh"', text) is not None
    assert sets_directly or inherits, (
        f"{path.name} neither sets strict mode nor sources common.sh (which does)"
    )


def test_common_sh_sets_strict_mode():
    """Every other script inherits it from here, so this is the single point of truth."""
    assert "set -euo pipefail" in (ROOT / "scripts" / "common.sh").read_text()


@pytest.mark.parametrize(
    "script,needle",
    [
        ("teardown.sh", "confirm_destructive"),
        ("teardown.sh", "solution_installed"),
        ("update.sh", "solution_installed"),
        ("bootstrap.sh", "solution_installed"),
    ],
)
def test_state_aware_and_guarded(script, needle):
    """Each lifecycle script must check what is actually installed before acting, and
    teardown must gate its irreversible step behind a typed confirmation."""
    assert needle in (ROOT / "scripts" / script).read_text()


def test_teardown_never_deletes_without_confirmation():
    """`pac solution delete` must only ever be reachable through the typed guard."""
    text = (ROOT / "scripts" / "teardown.sh").read_text()
    delete_at = text.index("pac solution delete")
    guard_at = text.index("confirm_destructive")
    assert guard_at < delete_at, "the destructive confirmation must precede the delete"


def test_teardown_warns_before_losing_the_sync_map():
    """Uninstalling first destroys the only record of which Google events belong to
    the automation, which is the mistake this warning exists to prevent."""
    text = (ROOT / "scripts" / "teardown.sh").read_text()
    assert "o365gcal-key" in text, "must tell the user how to find orphaned events"
    assert "FIRST" in text or "before" in text.lower()


def test_bootstrap_defaults_to_dry_run():
    """A first install must not write to a real Google calendar unattended."""
    text = (ROOT / "scripts" / "bootstrap.sh").read_text()
    assert '"o3gc_DryRun": "1"' in text


def test_update_refuses_when_not_installed():
    text = (ROOT / "scripts" / "update.sh").read_text()
    assert "bootstrap.sh" in text, "must redirect a first-time user to bootstrap"


def test_no_script_references_a_removed_script():
    """A dangling reference in docs or a Makefile target is a dead end for a user."""
    existing = {p.name for p in SCRIPTS}
    sources = list(SCRIPTS) + [ROOT / "Makefile"] + list((ROOT / "docs").glob("*.md"))
    sources.append(ROOT / "README.md")
    for src in sources:
        for ref in set(re.findall(r"scripts/([a-z-]+\.sh)", src.read_text())):
            assert ref in existing, f"{src.name} references missing scripts/{ref}"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_no_inline_python_c_blocks(path):
    """Ban `python3 -c '...'` in favour of a quoted heredoc.

    Inside a single-quoted shell string, `\\"` is not an escape - the backslash reaches
    Python literally and the program dies with a SyntaxError. Two scripts shipped with
    exactly that bug. A `<<'PY'` heredoc passes the body through untouched, so the
    failure mode cannot occur.
    """
    text = path.read_text()
    assert "python3 -c '" not in text and 'python3 -c "' not in text, (
        f"{path.name}: use `python3 - <<'PY' ... PY` instead of `python3 -c`"
    )


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_embedded_python_compiles(path):
    """Extract each quoted heredoc that looks like Python and compile it, so a syntax
    error surfaces here rather than halfway through someone's install."""
    import re

    text = path.read_text()
    for marker in ("PY", "PYFIX"):
        for block in re.findall(rf"<<'{marker}'\n(.*?)\n{marker}\n", text, re.S):
            compile(block, f"{path.name}:<<{marker}", "exec")


LIFECYCLE_SAFETY = ["update.sh", "teardown.sh", "restore.sh", "backup.sh"]


@pytest.mark.parametrize("script", LIFECYCLE_SAFETY)
def test_lifecycle_scripts_never_mutate_a_calendar(script):
    """The stated guarantee is that these scripts never create, change or delete a
    calendar event.

    Reading a connection listing or switching flows back on is fine - resuming normal
    operation is the point of a restore. What is banned is issuing a calendar write
    directly."""
    text = (ROOT / "scripts" / script).read_text()
    for forbidden in ("CreateEvent", "UpdateEvent", "DeleteEvent",
                      "googleapis.com", "/calendars/"):
        assert forbidden not in text, f"{script} performs a calendar operation: {forbidden}"


@pytest.mark.parametrize("script", ["update.sh", "teardown.sh", "restore.sh"])
def test_lifecycle_scripts_state_the_calendar_guarantee(script):
    """A user about to remove things needs to know what survives, in the script's own
    output - not buried in documentation they will not read at that moment."""
    text = (ROOT / "scripts" / script).read_text().lower()
    assert "calendar" in text
    assert any(w in text for w in ("never", "untouched", "safe", "remain")), (
        f"{script} must say plainly what happens to already-mirrored events"
    )


def test_teardown_does_not_delete_state_lists():
    """The sync map must outlive the solution: deleting it is what makes a reinstall
    duplicate every event, and the user cannot undo that."""
    text = (ROOT / "scripts" / "teardown.sh").read_text()
    assert "getbytitle" not in text, "teardown must not issue list operations"
    assert "not deleted here" in text


def test_teardown_offers_a_backup_before_deleting():
    text = (ROOT / "scripts" / "teardown.sh").read_text()
    assert text.index("backup.sh") < text.index("pac solution delete"), (
        "the backup offer must come before the destructive step"
    )


def test_update_preserves_flow_states():
    """An import deactivates every flow it replaces, so an upgrade that does not
    reactivate them leaves a silently dead mirror."""
    text = (ROOT / "scripts" / "update.sh").read_text()
    assert "WERE_ON" in text and "enable-flows.sh" in text


def test_update_takes_a_backup_by_default():
    text = (ROOT / "scripts" / "update.sh").read_text()
    assert "DO_BACKUP=1" in text and "--no-backup" in text


def test_restore_writes_the_sync_map_before_starting_flows():
    """Ordering is the point: a reconcile against an empty map re-mirrors everything."""
    text = (ROOT / "scripts" / "restore.sh").read_text()
    assert text.index("Restoring the sync map") < text.index("Restoring flow states")


#: zsh ties these lowercase parameters to their uppercase scalar counterparts.
#: Declaring one `local` and assigning a string to it wipes the real variable for the
#: rest of the scope - `local path` empties PATH and every external command in the
#: function fails with "command not found".
ZSH_TIED_PARAMETERS = ("path", "cdpath", "fpath", "manpath", "fignore", "mailpath")


@pytest.mark.parametrize("path_", SCRIPTS, ids=lambda p: p.name)
def test_no_local_shadowing_of_zsh_tied_parameters(path_):
    import re

    text = path_.read_text()
    for decl in re.findall(r"^\s*(?:local|typeset)\s+([^\n=]+)$", text, re.M):
        names = [n for n in decl.split() if not n.startswith("-")]
        for bad in ZSH_TIED_PARAMETERS:
            assert bad not in names, (
                f"{path_.name}: `local {bad}` shadows a zsh-tied parameter; "
                f"assigning to it breaks the shell for the rest of the function"
            )


NEGATIVE_INFERENCE_SCRIPTS = ["show-state.sh", "find-sharepoint-site.sh", "backup.sh"]


@pytest.mark.parametrize("script", NEGATIVE_INFERENCE_SCRIPTS)
def test_no_absence_claimed_from_a_failed_read(script):
    """A failed or empty read must not be reported as proof the thing is absent.

    This project made that mistake three times: a Graph permission limit reported as
    "you have no OneDrive", an error body counted as "0 rows, ok", and an
    unenumerable list collection reported as "NOT PRESENT". Each sent someone chasing
    a problem that did not exist, or hid one that did. Silence is not success, and an
    empty result is not evidence."""
    raw = (ROOT / "scripts" / script).read_text()
    assert "INCONCLUSIVE" in raw or "could not read" in raw or "cannot see" in raw, (
        f"{script} must distinguish 'could not determine' from 'does not exist'"
    )
    # Comment lines are excluded: the reasoning for avoiding a phrasing legitimately
    # quotes it, and linting the explanation would push it out of the code.
    code = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    for overreach in ("no personal site found", "NOT PRESENT"):
        assert overreach not in code, f"{script} asserts absence from a failed read"


def test_bootstrap_reconciles_a_saved_settings_file():
    """A saved settings file goes stale the moment the solution's variable set changes.
    An entry naming a variable the solution no longer declares fails the entire import
    with "some references included in the solution are not present in the
    organization" - which is what happened on the first reinstall after a variable was
    removed."""
    text = (ROOT / "scripts" / "bootstrap.sh").read_text()
    assert "reconcile_settings" in text
    assert text.index("reconcile_settings") < text.index("pac solution import")


def test_update_reconciles_settings_too():
    text = (ROOT / "scripts" / "update.sh").read_text()
    assert "reconcile_settings" in text
    assert text.index("reconcile_settings") < text.index("pac solution import")


def test_reconcile_drops_unknown_variables_and_reports_them():
    """Silently dropping configuration would be worse than failing: the installer
    should know a value they set is no longer used."""
    text = (ROOT / "scripts" / "common.sh").read_text()
    assert "dropped (no longer in the solution)" in text
    assert "taking the solution default for" in text


def test_a_single_command_install_exists():
    """The friction observed in practice was not any one step but their number:
    finding a SharePoint site, deriving a OneDrive path, pasting a calendar
    identifier out of a wall of JSON, running a flow by hand, switching flows on
    through a portal that hides the control. install.sh does all of it."""
    assert (ROOT / "scripts" / "install.sh").exists()
    text = (ROOT / "scripts" / "install.sh").read_text()
    for step in ("enable-flows.sh", "run-flow.sh", "configure.sh"):
        assert step in text, f"install.sh should handle {step} for the user"
    assert 'configure.sh" calendar' in text, (
        "the installer should let the user pick a calendar from a list rather than "
        "paste an identifier"
    )


def test_install_practises_before_writing_to_a_calendar():
    """Nothing should reach a real calendar before the installer has seen the plan."""
    text = (ROOT / "scripts" / "install.sh").read_text()
    assert '"o3gc_DryRun": "yes"' in text
    assert text.index('"o3gc_DryRun": "yes"') < text.index("dryrun off")
    assert "Start mirroring for real?" in text


def test_install_speaks_plainly():
    """Written for someone who has never opened Power Automate. Jargon in the prompts
    is what sends people back to asking a colleague."""
    text = (ROOT / "scripts" / "install.sh").read_text()
    for jargon in ("environment variable", "connection reference", "Dataverse",
                   "solution zip", "OpenApiConnection"):
        assert jargon not in text, f"install.sh says '{jargon}' to the user"


def test_configure_offers_plain_language_toggles():
    text = (ROOT / "scripts" / "configure.sh").read_text()
    for short in ("dryrun", "notify", "private", "calendar", "window"):
        assert short in text, f"configure.sh should expose '{short}'"
    assert "NOT INSTALLED" in text, (
        "a setting missing from the environment must not render as merely 'off'"
    )
