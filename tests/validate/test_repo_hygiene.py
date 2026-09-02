"""Keeps one organisation's configuration out of a repository meant to be shared.

A backup directory was committed containing the installer's email address, their
SharePoint personal-site URL, the Dataverse org host, the tenant id and their
connection ids. No credentials - but a solution intended to be handed to colleagues
should not carry the first installer's environment inside it.

These checks look at what is *tracked*, not what exists: build output and per-user
configuration are expected on disk and expected to be ignored.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("not a git repository")
    return [line for line in out.stdout.splitlines() if line]


def is_ignored(path: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", path],
                          cwd=ROOT).returncode == 0


#: Paths that must never be tracked, with what each would expose.
MUST_BE_IGNORED = {
    "backups/x/settings.json": "an installer's environment variable values",
    "backups/x/connections.txt": "connection ids for one tenant",
    "o365gcal.settings.json": "the installer's site URL, email and connection ids",
    "settings.json": "same, under the name a backup writes",
    "connectors/shared_office365.json": "large and regenerable",
    "dist/O365GCal_managed.zip": "build output",
    "solution/out/O365GCal_managed.zip": "build output",
    "pac-log.txt": "CLI diagnostics, which quote request bodies",
}


@pytest.mark.parametrize("path,why", sorted(MUST_BE_IGNORED.items()))
def test_path_is_ignored(path, why):
    assert is_ignored(path), f"{path} is not ignored, and would expose {why}"


def test_no_backup_directory_is_tracked():
    assert not [p for p in tracked() if p.startswith("backups/")]


#: Strings that identify one specific tenant. Example hostnames are fine in docs -
#: the point is to catch real ones.
TENANT_MARKERS = {
    r"[a-z0-9-]+-my\.sharepoint\.com/personal/": "a real OneDrive personal-site URL",
    r"[a-z0-9]+\.crm\.dynamics\.com": "a real Dataverse organisation host",
    r"shared-(googlecalenda|office365|sharepointonl)[a-z0-9-]*-[0-9a-f]{8}-": "a real connection id",
    r"Default-[0-9a-f]{8}-[0-9a-f]{4}-": "a real tenant/environment id",
    r"c_[0-9a-f]{60,}@group\.calendar\.google\.com": "a real Google calendar id",
}

#: Placeholders that deliberately look like the real thing.
ALLOWED = ("contoso", "CONTOSO", "example.com", "<your", "princetonu.sharepoint.com/sites/O365GCal")


@pytest.mark.parametrize("pattern,what", sorted(TENANT_MARKERS.items()))
def test_no_tenant_identifiers_are_tracked(pattern, what):
    rx = re.compile(pattern)
    offenders = []
    for rel in tracked():
        path = ROOT / rel
        try:
            text = path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            if rx.search(line) and not any(a in line for a in ALLOWED):
                offenders.append(f"{rel}: {line.strip()[:110]}")
    assert not offenders, f"tracked file(s) contain {what}:\n" + "\n".join(offenders[:10])


#: Domains reserved for documentation and examples. Anything else is somebody real.
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net", "contoso.com")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: Addresses that are structurally email-shaped but are not addresses.
_NOT_ADDRESSES = ("@group.calendar.google.com", "@example", "@{", "o365gcal-key")


def test_no_real_email_addresses_are_tracked():
    """No tracked file may contain an address outside the reserved example domains.

    Stated as a general rule rather than a search for one person's address, because
    the first attempt embedded the address it was looking for - so the guard itself
    committed the thing it existed to prevent, and flagged its own source file.
    """
    offenders = []
    for rel in tracked():
        try:
            text = (ROOT / rel).read_text(errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for hit in _EMAIL.findall(line):
                if any(marker in hit for marker in _NOT_ADDRESSES):
                    continue
                if hit.lower().endswith(EXAMPLE_DOMAINS):
                    continue
                offenders.append(f"{rel}:{lineno}: {hit}")
    assert not offenders, (
        "tracked file(s) contain non-example email address(es); use an example.com "
        "address in fixtures and documentation:\n" + "\n".join(sorted(set(offenders))[:10])
    )


def test_gitignore_covers_both_settings_spellings():
    """`*.settings.json` matches only names ending in that. A plain `settings.json`
    inside a backup directory is what actually got committed."""
    text = (ROOT / ".gitignore").read_text()
    assert "*.settings.json" in text
    assert re.search(r"^settings\.json$", text, re.M), (
        "also ignore a bare settings.json"
    )
