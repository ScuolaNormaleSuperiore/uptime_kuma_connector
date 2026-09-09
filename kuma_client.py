"""Pure logic for reading Uptime Kuma. Imports nothing from `cat`, does no I/O.

Two rules govern this module and are the reason it exists separately:

- it never imports from `cat`, so it can be exercised with `pytest` alone
- it never performs I/O: the adapter fetches, this module parses and decides

The adapter catches every failure before it can escape into a conversation.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# Uptime Kuma's own status codes, as they appear in the `monitor_status` metric.
# All four are kept distinct on purpose: collapsing them into up/down would
# announce planned maintenance as a fault.
STATUS_DOWN = 0
STATUS_UP = 1
STATUS_PENDING = 2
STATUS_MAINTENANCE = 3

# The four outcomes the module can return. `unknown` is not an error state: it
# is the honest answer when Uptime Kuma cannot be read, and keeping it distinct
# from `down` is the point of the whole design. `not_monitored` is a *separate*
# answer and never a synonym: "no check exists for that service" is certain,
# while `unknown` is our own malfunction.
OUTCOME_KNOWN = "known"
OUTCOME_NOT_MONITORED = "not_monitored"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_UNKNOWN = "unknown"

# Above this many matching monitors the request does not identify a service, and
# the outcome is `ambiguous` rather than a sentence carrying dozens of names
# into the prompt. A project value, to revisit against a real instance.
MAXIMUM_REPORTED_MATCHES = 5

# An alias is an explicit administrator decision, not a guess, so it gets its
# own — higher — ceiling instead of sharing the one above: grouping the
# components of one composite service under an alias is intended, and must not
# turn into `ambiguous` just because there happen to be more than five of them.
MAXIMUM_ALIAS_MATCHES = 10

MAXIMUM_SERVICE_NAME_LENGTH = 80

# Monitor names come from an external HTTP response. Keep a separate, slightly
# larger bound from the model-supplied service name: valid descriptive names
# remain usable for matching, while an attacker cannot carry an arbitrary
# amount of text into diagnostics.
MAXIMUM_MONITOR_NAME_LENGTH = 160

# Below this many characters, plain substring containment is skipped: a one-
# or two-character normalised query is a substring of almost any name (e.g.
# "a" inside "archivio"), matching monitors the query does not identify at
# all. A whole-word match — the fallback below — still goes through regardless
# of length, so a short query that is genuinely one whole word of a monitor
# name (an acronym such as "PA") is unaffected.
MINIMUM_CONTAINMENT_LENGTH = 3

# Removed before comparing names, so "UGOV" finds "U-GOV - Autenticazione" on
# its own and a whole class of aliases never has to be written by hand.
_INSIGNIFICANT_CHARACTERS = str.maketrans("", "", "-._")

# This text comes from the model and reaches both a sentence for the user and
# `log.warning()` calls in the adapter, verbatim. Stripped rather than merely
# truncated, so a newline in a crafted service name cannot forge what looks
# like a second, unrelated log line.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

# A monitor name is never expected to be a URL. Schemes are matched generally
# rather than enumerating http/https so uncommon URL forms cannot bypass the
# boundary check.
_URL_IN_MONITOR_NAME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://|\bwww\.", re.I)

# Prometheus label names are identifiers and label values may escape a quote,
# backslash, or newline. Parsing labels separately avoids coupling the monitor
# fields to an order that Uptime Kuma does not promise.
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:\\.|[^"\\])*)"')
_MONITOR_STATUS_LINE = re.compile(
    r"^monitor_status\{(.*)\}\s+(\S+)(?:\s+\S+)?\s*$"
)


@dataclass(frozen=True)
class MonitorResolution:
    """The result of resolving a requested service against monitor names.

    `missing_alias_ids` is kept separate from an ordinary empty match: an alias
    with an invalid id is a configuration failure, not proof that the requested
    service is unmonitored. The adapter will log partial misses; `describe_status`
    will turn a total alias miss into `unknown`.
    """

    requested_name: str
    matches: tuple[tuple[int, str], ...]
    used_alias: bool
    missing_alias_ids: tuple[int, ...]


def normalise_name(value: str) -> str:
    """Fold a service name to the form every comparison is made on.

    Applied to all three sides — what the user asked, the monitor names, and the
    alias keys — so they are always compared in the same shape.

    The order is load-bearing: the insignificant characters go first, because
    removing them from "U-GOV - Autenticazione" leaves a double space that the
    whitespace pass then has to collapse.
    """
    without_punctuation = str(value or "").translate(_INSIGNIFICANT_CHARACTERS)
    return " ".join(without_punctuation.split()).casefold()


def parse_alias_map(text: str) -> tuple[dict[str, tuple[int, ...]], tuple[str, ...]]:
    """Read the admin panel's alias map into `{normalised alias: monitor ids}`.

    Returns `(aliases, problems)`. The problems are human-readable lines for the
    adapter to log: this module does no I/O, so it reports rather than logs.

    Format, one entry per line, aliases left of the colon and monitor ids right
    of it:

        U-GOV, UGOV, Ugov: 12
        Esse3, Segreteria online: 14, 15, 16
        # a comment
        VPN: 17

    **A malformed line is discarded on its own and never abandons the rest.**
    That is the whole reason this function exists instead of a one-line parse:
    the core reads `requirements.txt` by calling `packaging.Requirement()` in a
    loop inside a `try` that gives up on the entire list at the first bad line,
    installing nothing at all. The same mistake here would disable the
    resolution of every service because of one stray character.

    Several ids for one alias is intended, not tolerated: it is how an
    administrator groups the components of a service explicitly, without
    depending on how they were named. It resolves to a multiple match. A
    **repeated** id on the same line collapses to one, silently: a copy-paste
    duplicate is not a second component to report twice.

    A conflicting alias keeps the **first** definition, and the later one is
    reported: silently overriding would make the order of the lines a hidden
    rule. A *harmless* duplicate is not reported at all — writing both "U-GOV"
    and "UGOV" on one line is the natural thing to do, and normalisation makes
    them the same key with the same ids. Only a repeat that would resolve to
    different monitors is a problem worth a log line.
    """
    aliases: dict[str, tuple[int, ...]] = {}
    problems: list[str] = []

    for number, raw_line in enumerate(str(text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if ":" not in line:
            problems.append(f"line {number}: no ':' separating aliases from ids")
            continue

        raw_aliases, raw_ids = line.split(":", 1)

        names = [normalise_name(part) for part in raw_aliases.split(",")]
        names = [name for name in names if name]
        if not names:
            problems.append(f"line {number}: no alias before the ':'")
            continue

        monitor_ids: list[int] = []
        malformed_id = None
        for part in raw_ids.split(","):
            candidate = part.strip()
            if not candidate:
                continue
            try:
                monitor_ids.append(int(candidate))
            except ValueError:
                malformed_id = candidate
                break

        # One bad id discards the line rather than half of it: a partially
        # applied alias would answer about some components of a service and stay
        # silent about others, which is worse than not applying it.
        if malformed_id is not None:
            problems.append(f"line {number}: '{malformed_id}' is not a monitor id")
            continue

        if not monitor_ids:
            problems.append(f"line {number}: no monitor id after the ':'")
            continue

        # Repeated ids on one line collapse to a single match, in the order
        # first seen: a copy-paste duplicate must not turn into a duplicated
        # clause in the sentence, or trip the ambiguous-match ceiling for what
        # is really one monitor.
        monitor_ids = list(dict.fromkeys(monitor_ids))

        for name in names:
            existing = aliases.get(name)
            if existing is not None:
                if existing != tuple(monitor_ids):
                    problems.append(
                        f"line {number}: alias '{name}' already points at "
                        f"{list(existing)}, this line is ignored"
                    )
                continue
            aliases[name] = tuple(monitor_ids)

    return aliases, tuple(problems)


def metrics_url(base_url: str) -> str:
    """The single endpoint this plugin reads.

    `/metrics` is preferred over the status-page endpoints because one line
    carries name, id and status together, and because the alternative
    requires a *published* status page.

    The settings validator owns URL normalisation and removes a trailing slash;
    this function only appends the endpoint. An empty URL stays empty so a
    caller cannot accidentally turn an unusable configuration into `/metrics`.
    """
    if not base_url:
        return ""
    return f"{base_url}/metrics"


def basic_auth_header(api_key: str) -> str:
    """The Authorization header value for `/metrics`.

    The form is a convention of Uptime Kuma and is not obvious: basic auth with
    an **empty username** and the API key as the password,
    `base64(":" + api_key)`.

    An empty key returns an empty value so the adapter can recognise an
    unusable configuration without constructing an authentication header.
    """
    if not api_key:
        return ""

    credentials = f":{api_key}".encode("utf-8")
    encoded_credentials = base64.b64encode(credentials).decode("ascii")
    return f"Basic {encoded_credentials}"


def _parse_prometheus_labels(text: str) -> dict[str, str] | None:
    """Parse one `{key="value",...}` body, rejecting malformed input."""
    labels: dict[str, str] = {}
    position = 0

    while position < len(text):
        match = _LABEL.match(text, position)
        if match is None:
            return None

        key, raw_value = match.groups()
        value: list[str] = []
        escaped = False
        for character in raw_value:
            if escaped:
                value.append("\n" if character == "n" else character)
                escaped = False
            elif character == "\\":
                escaped = True
            else:
                value.append(character)
        if escaped:
            return None

        labels[key] = "".join(value)
        position = match.end()
        if position == len(text):
            break
        if text[position] != ",":
            return None
        position += 1

    return labels


def parse_monitor_metrics(payload: str) -> tuple[dict[int, str], dict[int, int]]:
    """Read `monitor_status` lines into names and statuses, both keyed by id.

    Returns `(names, statuses)`. The labels are parsed without assuming either
    their order or their complete set: tags and transport-specific fields are
    expected to vary between monitors.

    The lines look like:

        monitor_status{tag="",monitor_id="12",monitor_name="VPN",monitor_type="http"} 1

    A malformed line or a missing label is skipped, never raised on. The label
    format is the part most easily assumed wrong, which is why the fixtures for
    this function must come from a real response.
    """
    names: dict[int, str] = {}
    statuses: dict[int, int] = {}

    for raw_line in str(payload or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("monitor_status{"):
            continue

        match = _MONITOR_STATUS_LINE.fullmatch(line)
        if match is None:
            continue

        labels = _parse_prometheus_labels(match.group(1))
        if labels is None:
            continue

        try:
            monitor_id = int(labels["monitor_id"])
            monitor_name = labels["monitor_name"]
            status = int(match.group(2))
        except (KeyError, TypeError, ValueError):
            continue

        if not _is_safe_monitor_name(monitor_name):
            continue

        names[monitor_id] = monitor_name
        statuses[monitor_id] = status

    return names, statuses


def _is_safe_monitor_name(value: str) -> bool:
    """Accept external monitor text only when it is safe to retain and log.

    Unicode controls and format characters include newlines, bidirectional
    overrides and invisible separators. Dropping the whole metric is safer
    than silently changing its identity and accidentally matching a different
    service. URLs and excessive text have no legitimate role in a monitor
    name and are rejected at the same boundary.
    """
    if not value or len(value) > MAXIMUM_MONITOR_NAME_LENGTH:
        return False
    if _URL_IN_MONITOR_NAME.search(value):
        return False
    return not any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in value
    )


def find_monitor(
    names: Mapping[int, str],
    wanted: str,
    explicit_ids: Mapping[str, tuple[int, ...]] | None = None,
) -> MonitorResolution:
    """Resolve what the user typed to every matching monitor.

    The request is limited to 80 characters before it is retained for a
    response sentence. Resolution order is an explicit alias, an exact name,
    then containment in either direction, including the same normalised words
    in a different order. Every match is returned: the caller reports up to
    `MAXIMUM_REPORTED_MATCHES` and produces the `ambiguous` outcome above that
    limit — `MAXIMUM_ALIAS_MATCHES` instead, when the match came from an alias.
    """
    requested_name = _truncate_service_name(wanted)
    normalised_wanted = normalise_name(requested_name)
    empty = MonitorResolution(requested_name, (), False, ())
    if not normalised_wanted:
        return empty

    aliases = explicit_ids or {}
    alias_ids = aliases.get(normalised_wanted)
    if alias_ids is not None:
        matches = tuple(
            (monitor_id, names[monitor_id])
            for monitor_id in alias_ids
            if monitor_id in names
        )
        missing_ids = tuple(
            monitor_id for monitor_id in alias_ids if monitor_id not in names
        )
        return MonitorResolution(requested_name, matches, True, missing_ids)

    exact_matches = tuple(
        (monitor_id, name)
        for monitor_id, name in names.items()
        if normalise_name(name) == normalised_wanted
    )
    if exact_matches:
        return MonitorResolution(requested_name, exact_matches, False, ())

    containment_matches = tuple(
        (monitor_id, name)
        for monitor_id, name in names.items()
        if _names_contain_each_other(normalised_wanted, normalise_name(name))
    )
    return MonitorResolution(requested_name, containment_matches, False, ())


def _names_contain_each_other(left: str, right: str) -> bool:
    """Match normalised names by phrase containment or by their word sets.

    Plain substring containment requires both sides to be at least
    `MINIMUM_CONTAINMENT_LENGTH` characters — otherwise a short query would
    match by coincidence inside unrelated names, such as a monitor whose name
    happens to share one common short syllable with it.
    """
    # A punctuation-only monitor name normalises to an empty string. Without
    # this guard its empty word set is a subset of every requested name, so it
    # would produce a false match for every query. Keep the monitor available
    # to an explicit id-based alias; only heuristic name matching is refused.
    if not left or not right:
        return False

    if (
        len(left) >= MINIMUM_CONTAINMENT_LENGTH
        and len(right) >= MINIMUM_CONTAINMENT_LENGTH
        and (left in right or right in left)
    ):
        return True
    return set(left.split()).issubset(right.split()) or set(right.split()).issubset(
        left.split()
    )


def _truncate_service_name(value: str) -> str:
    """Bound text that may later be included in a sentence, or a log line.

    Control characters are replaced with a space before truncating: this text
    is untrusted (it comes from the model) and this is the one place every
    caller — the sentence builder and the adapter's diagnostic logging alike —
    goes through, so it is the one place a newline can be stopped from forging
    a second, unrelated log line.
    """
    text = _CONTROL_CHARACTERS.sub(" ", str(value or ""))
    return text[:MAXIMUM_SERVICE_NAME_LENGTH]


def monitored_service_names(names: Mapping[int, str]) -> Sequence[str]:
    """Every monitor name, for diagnosing a miss in the adapter log.

    These names never enter the `not_monitored` sentence: offering the model
    other services could make it answer about the wrong one. The adapter may
    instead log at most three close names for an administrator.
    """
    return tuple(names.values())


def describe_resolution(
    resolution: MonitorResolution, statuses: Mapping[int, int]
) -> tuple[str, str]:
    """Turn an already-resolved match into one sentence for the model.

    `describe_status()` below calls this after parsing `/metrics` and
    resolving the request itself, and is the function to use when both steps
    still have to happen. This one exists for a caller that already did
    them — the adapter, which needs the same `MonitorResolution` again to log
    administrator diagnostics — so it is not forced to parse the payload and
    resolve the request a second time just to get the sentence.

    Returns `(outcome, sentence)`, where the outcome is one of the four
    constants above and the sentence is what reaches the model. A broken alias
    or an unrecognised status is `unknown`: neither may be misreported as a
    working service.
    """
    if resolution.used_alias and not resolution.matches:
        return OUTCOME_UNKNOWN, unreachable_sentence(resolution.requested_name)

    if not resolution.matches:
        return (
            OUTCOME_NOT_MONITORED,
            "Non risulta alcun controllo di disponibilità per "
            f"{resolution.requested_name}. Non è disponibile alcuna informazione "
            "sul suo stato: non concluderne che il servizio funzioni.",
        )

    match_limit = MAXIMUM_ALIAS_MATCHES if resolution.used_alias else MAXIMUM_REPORTED_MATCHES
    if len(resolution.matches) > match_limit:
        return (
            OUTCOME_AMBIGUOUS,
            f"{resolution.requested_name} corrisponde a troppi controlli per "
            "identificare un servizio. Chiedi all'utente quale servizio intende.",
        )

    clauses: list[str] = []
    for monitor_id, _monitor_name in resolution.matches:
        clause = _status_clause(statuses.get(monitor_id))
        if clause is None:
            return OUTCOME_UNKNOWN, unreachable_sentence(resolution.requested_name)
        clauses.append(clause)

    if len(clauses) == 1:
        return OUTCOME_KNOWN, f"Il servizio {resolution.requested_name} {clauses[0]}."

    # Monitor names are external data and never enter the model context. For a
    # composite service, stable ordinal labels retain the per-check states
    # without exposing a name that could contain prompt-like instructions.
    reported = "; ".join(
        f"controllo {number} {clause}"
        for number, clause in enumerate(clauses, start=1)
    )
    return (
        OUTCOME_KNOWN,
        f"Per {resolution.requested_name} risultano più controlli: {reported}.",
    )


def describe_status(
    payload: str,
    service_name: str,
    explicit_ids: Mapping[str, tuple[int, ...]] | None = None,
) -> tuple[str, str]:
    """Parse `/metrics`, resolve the request, and turn it into one sentence.

    Returns `(outcome, sentence)`. Convenience wrapper around
    `parse_monitor_metrics()`, `find_monitor()` and `describe_resolution()` for
    a caller — tests, or any future one-shot caller — that has no reason to
    keep the intermediate `names`/`statuses`/`MonitorResolution` around.
    """
    names, statuses = parse_monitor_metrics(payload)
    if not names:
        return OUTCOME_UNKNOWN, unreachable_sentence(service_name)

    resolution = find_monitor(names, service_name, explicit_ids)
    return describe_resolution(resolution, statuses)


def _status_clause(status: int | None) -> str | None:
    """Map a known Uptime Kuma status to its Italian sentence clause."""
    return {
        STATUS_UP: "risulta attivo",
        STATUS_DOWN: "non risulta attualmente attivo",
        STATUS_PENDING: "presenta rilevazioni intermittenti",
        STATUS_MAINTENANCE: "è in manutenzione programmata",
    }.get(status)


def unreachable_sentence(service_name: str) -> str:
    """What to tell the model when `/metrics` could not be read at all.

    It states that the status is not verifiable **and** instructs the model not
    to conclude anything: asked "is the VPN down?", a model fills a silence if
    it is allowed to. This is the invariant of the whole plugin — never invent
    a state.
    """
    return (
        f"Non è stato possibile determinare lo stato di {_truncate_service_name(service_name)}. "
        "Non trarre conclusioni: non affermare né che il servizio è attivo né "
        "che è guasto."
    )
