"""Normalisation, correlation keys and content hashing.

The *fingerprint* is the heart of the whole system. It answers "has this event
actually changed?" in one comparison, which is what keeps the reconciler inside the
Google connector's 100-calls-per-60-seconds budget and what suppresses the update
storms caused by Exchange rewriting `lastModifiedDateTime` for its own internal
reasons.

Why a plain string rather than a hash: the Power Automate expression language has no
hashing function of any kind. A flow therefore cannot compute SHA-256, so the value
stored on the sync-map row has to be something the flow can build and compare
directly. `fingerprint()` is that string and is the normative definition;
`content_hash()` is a convenience for Python callers and hashes exactly the same
bytes, so the two agree on every comparison.

Two further rules matter here and are easy to get wrong:

1. Hash the *inputs*, never the rendered description. The renderer's formatting is
   ours to change; if it were hashed, a cosmetic tweak would rewrite every mirrored
   event on the next run.
2. Correlate on `iCalUId`, not the Graph event `id`. Outlook rewrites the event `id`
   when a user accepts an invitation -- the documented cause of the trigger firing
   twice for one meeting -- whereas `iCalUId` survives it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from .model import Config, OutlookEvent, Sensitivity

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(p|div|br|tr|li|h[1-6])\s*/?>", re.IGNORECASE)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)

#: Marker appended to every mirrored Google description. Purely a repair hatch:
#: if the SharePoint map is lost, orphans can be found via ListEvents `q=`. It is
#: never on the hot path, because Google's text index is not read-your-writes
#: consistent and a freshly created event may not be findable for some minutes.
MARKER_PREFIX = "o365gcal-key:"

#: How much of the raw body participates in the fingerprint.
#:
#: The connector gives us the body as raw HTML with no plain-text alternative, and a
#: flow cannot strip markup (no regex). So the fingerprint uses the total length plus
#: a bounded prefix of the raw string.
#:
#: The deliberate trade: Exchange rewrites body HTML -- styles, tracking spans -- with
#: no user edit, so this occasionally reports a change that is not one, producing a
#: redundant but harmless idempotent overwrite. The alternative, leaving the body out
#: entirely, would silently miss real body edits. A spurious write costs one API call;
#: a missed edit is a correctness gap, so the noisy option wins.
BODY_FINGERPRINT_CHARS = 256


def to_utc(value: datetime) -> datetime:
    """Coerce to timezone-aware UTC. Naive input is *assumed* UTC, matching what the
    Outlook connector emits for `startDateTimeUtc` style fields."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    """Second-precision ISO-8601 Z. Sub-second precision is deliberately dropped:
    Exchange varies it between reads of an otherwise identical event, which would
    make the hash unstable and cause perpetual updates."""
    return to_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_html(raw: str) -> str:
    """Reduce an Outlook HTML body to comparable plain text.

    Outlook rewrites body HTML (styles, conditional comments, tracking spans)
    without the user changing anything, so the raw body is far too noisy to hash.
    """
    if not raw:
        return ""
    text = _BR.sub("\n", raw)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def normalize_attendees(required: str, optional: str) -> str:
    """Attendee fingerprint, built the only way a flow can build it.

    The connector hands over two semicolon-separated strings, and the expression
    language cannot split, sort or deduplicate them. So this is a lowercased
    concatenation and nothing more.

    That means attendee *reordering* by Exchange registers as a change. It produces a
    redundant idempotent overwrite, which is cheap; sorting here would look tidier and
    would silently break parity with the shipped flow, which is not cheap.
    """
    return f"{(required or '').strip().lower()};{(optional or '').strip().lower()}"


def correlation_key(ical_uid: str, occurrence_start: datetime) -> str:
    """Stable identity of one mirrored occurrence: `{iCalUId}|{startUtc}`.

    All occurrences of a series share an `iCalUId`, so the start time is what
    separates them. This survives the event-id rewrite on invitation acceptance.
    """
    return f"{ical_uid}|{iso_utc(occurrence_start)}"


#: TitlePrefix sentinel meaning "no prefix". An empty default cannot be resolved by
#: the runtime at all, and a whitespace-only one is normalised away on import, so the
#: absence of a prefix has to be spelled out.
NO_PREFIX = "none"


def title_prefix(config: Config) -> str:
    """The configured prefix, with the sentinel mapped to nothing."""
    prefix = config.title_prefix or ""
    return "" if prefix.strip().lower() == NO_PREFIX else prefix


def effective_subject(event: OutlookEvent, config: Config) -> str:
    """Subject as it should appear on Google, honouring privacy and title prefix."""
    if _is_hidden(event, config):
        return "Busy"
    return f"{title_prefix(config)}{event.subject}".strip()


def _is_hidden(event: OutlookEvent, config: Config) -> bool:
    """Whether this event mirrors as an opaque block.

    The time is still reserved on Google, without leaking the subject, body or
    attendees.
    """
    return config.hide_private_event_details and event.sensitivity in (
        Sensitivity.PRIVATE,
        Sensitivity.CONFIDENTIAL,
    )


def body_fingerprint(body_html: str) -> str:
    """Bounded representation of an event body.

    Line breaks and tabs are flattened to spaces so that pure line-ending churn does
    not register. Runs of spaces are deliberately *not* collapsed: that needs a regex,
    which the expression language does not have, so the flow could not compute the
    same value. Keeping the engine and the flow bit-identical matters more than
    absorbing a little extra whitespace noise.
    """
    text = (body_html or "").replace("\n", " ").replace("\r", " ").replace("\t", " ").strip()
    return f"{len(text)}:{text[:BODY_FINGERPRINT_CHARS]}"


def fingerprint(event: OutlookEvent, config: Config) -> str:
    """The normative change-detection value, stored on the sync-map row.

    Config fields that change rendered output (`title_prefix`,
    `hide_private_event_details`)
    participate on purpose: if an admin flips one, every event *should* be rewritten on
    the next reconcile. Deliberately absent: `lastModifiedDateTime`, the Graph event
    `id`, and anything else Exchange mutates on its own.

    Fields are joined with US (0x1f), a character Outlook content cannot contain, so
    no value can impersonate a field boundary.

    `is_cancelled` is absent on purpose: the calendar view does not return it, so the
    flow could not include it. Cancellation is detected by the occurrence vanishing
    from the read instead.
    """
    hidden = _is_hidden(event, config)
    fields = [
        effective_subject(event, config),
        iso_utc(event.start_utc),
        iso_utc(event.end_utc),
        "1" if event.is_all_day else "0",
        "" if hidden else (event.location or "").strip(),
        "" if hidden else body_fingerprint(event.body_html),
        (event.show_as or "").strip().lower(),
        event.sensitivity.value,
        "" if hidden else normalize_attendees(event.required_attendees, event.optional_attendees),
        "" if hidden else (event.organizer or "").strip().lower(),
        event.my_response.value,
        "1" if config.hide_private_event_details else "0",
    ]
    return "\x1f".join(fields)


def content_hash(event: OutlookEvent, config: Config) -> str:
    """SHA-256 of `fingerprint()`. Equivalent for comparison; compact for Python."""
    return hashlib.sha256(fingerprint(event, config).encode("utf-8")).hexdigest()
