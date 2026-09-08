"""Pure logic for reading Uptime Kuma. Imports nothing from `cat`, does no I/O.

This module is being implemented incrementally. Functions not reached yet keep
a neutral body, so the shape of the module matches the specification without
exposing unfinished behaviour. The specification is `DOC/Specifiche.md`; the
section numbers below point into it.

Two rules govern this module and are the reason it exists separately:

- it never imports from `cat`, so it can be exercised with `pytest` alone
- it never performs I/O: the adapter fetches, this module parses and decides

The bodies return neutral values rather than raising `NotImplementedError`. The
core executes plugin code without a safety net and runs with auto-reload, so an
exception from a path invoked by mistake becomes either an error shown to a user
or a failed activation. An inert body is safe; one that raises is a trap. To
make sure nothing invokes these by accident, the tool and the hook that would
call them are **not registered** in the adapter either.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# Uptime Kuma's own status codes, as they appear in the `monitor_status` metric.
# All four are kept distinct on purpose: collapsing them into up/down would
# announce planned maintenance as a fault. See Specifiche.md, section 2.4.
STATUS_DOWN = 0
STATUS_UP = 1
STATUS_PENDING = 2
STATUS_MAINTENANCE = 3

# The four outcomes the module can return. `unknown` is not an error state: it
# is the honest answer when Uptime Kuma cannot be read, and keeping it distinct
# from `down` is the point of the whole design. `not_monitored` is a *separate*
# answer and never a synonym: "no check exists for that service" is certain,
# while `unknown` is our own malfunction. See Specifiche.md, section 4.
OUTCOME_KNOWN = "known"
OUTCOME_NOT_MONITORED = "not_monitored"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_UNKNOWN = "unknown"

# Above this many matching monitors the request does not identify a service, and
# the outcome is `ambiguous` rather than a sentence carrying dozens of names
# into the prompt. A project value, to revisit against a real instance.
MAXIMUM_REPORTED_MATCHES = 5
MAXIMUM_SERVICE_NAME_LENGTH = 80

# Removed before comparing names, so "UGOV" finds "U-GOV - Autenticazione" on
# its own and a whole class of aliases never has to be written by hand.
_INSIGNIFICANT_CHARACTERS = str.maketrans("", "", "-._")

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
    alias keys — so they are always compared in the same shape. See
    Specifiche.md, section 3.2.

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
    depending on how they were named. It resolves to a multiple match.

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

    See Specifiche.md, section 2.1: `/metrics` is preferred over the status-page
    endpoints because one line carries name, id and status together, and
    because the alternative requires a *published* status page.

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
    `base64(":" + api_key)`. See Specifiche.md, section 2.3.

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
    this function must come from a real response. See Specifiche.md, 3.1 and 6.
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

        names[monitor_id] = monitor_name
        statuses[monitor_id] = status

    return names, statuses


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
    five and produces the `ambiguous` outcome above that limit.
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
    """Match normalised names by phrase containment or by their word sets."""
    if left in right or right in left:
        return True
    return set(left.split()).issubset(right.split()) or set(right.split()).issubset(
        left.split()
    )


def _truncate_service_name(value: str) -> str:
    """Bound text that may later be included in a sentence for the model."""
    return str(value or "")[:MAXIMUM_SERVICE_NAME_LENGTH]


def monitored_service_names(names: Mapping[int, str]) -> Sequence[str]:
    """Every monitor name, for diagnosing a miss in the adapter log.

    These names never enter the `not_monitored` sentence: offering the model
    other services could make it answer about the wrong one. The adapter may
    instead log at most three close names for an administrator. See
    Specifiche.md, section 3.4.
    """
    return tuple(names.values())


def describe_status(
    payload: str,
    service_name: str,
    explicit_ids: Mapping[str, tuple[int, ...]] | None = None,
) -> tuple[str, str]:
    """Turn a `/metrics` response into one sentence for the model.

    Returns `(outcome, sentence)`, where the outcome is one of the four
    constants above and the sentence is what reaches the model. An unparseable
    payload, a broken alias, or an unrecognised status is `unknown`: none may
    be misreported as a working service. See Specifiche.md, section 4.
    """
    requested_name = _truncate_service_name(service_name)
    names, statuses = parse_monitor_metrics(payload)
    if not names:
        return OUTCOME_UNKNOWN, unreachable_sentence(requested_name)

    resolution = find_monitor(names, requested_name, explicit_ids)
    if resolution.used_alias and not resolution.matches:
        return OUTCOME_UNKNOWN, unreachable_sentence(resolution.requested_name)

    if not resolution.matches:
        return (
            OUTCOME_NOT_MONITORED,
            "Non risulta alcun controllo di disponibilità per "
            f"{resolution.requested_name}. Non è disponibile alcuna informazione "
            "sul suo stato: non concluderne che il servizio funzioni.",
        )

    if len(resolution.matches) > MAXIMUM_REPORTED_MATCHES:
        return (
            OUTCOME_AMBIGUOUS,
            f"{resolution.requested_name} corrisponde a troppi controlli per "
            "identificare un servizio. Chiedi all'utente quale servizio intende.",
        )

    clauses: list[tuple[str, str]] = []
    for monitor_id, monitor_name in resolution.matches:
        clause = _status_clause(statuses.get(monitor_id))
        if clause is None:
            return OUTCOME_UNKNOWN, unreachable_sentence(resolution.requested_name)
        clauses.append((monitor_name, clause))

    if len(clauses) == 1:
        monitor_name, clause = clauses[0]
        return OUTCOME_KNOWN, f"Il servizio {monitor_name} {clause}."

    reported = "; ".join(f"{name} {clause}" for name, clause in clauses)
    return (
        OUTCOME_KNOWN,
        f"Per {resolution.requested_name} risultano più controlli: {reported}.",
    )


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
    a state. See Specifiche.md, section 4.
    """
    return (
        f"Non è stato possibile determinare lo stato di {_truncate_service_name(service_name)}. "
        "Non trarre conclusioni: non affermare né che il servizio è attivo né "
        "che è guasto."
    )
