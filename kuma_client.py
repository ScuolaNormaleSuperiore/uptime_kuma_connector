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

# Turned into spaces before comparing names, so that a hyphen, a dot or an
# underscore used as a word separator reads as one. Every comparison is then
# made twice, against this form and against the spaceless form built by
# `fuse_name()`: "Portale-XX" has to meet "Portale XX", which needs the
# separator to become a space, while "UGOV" has to meet "U-GOV - Something",
# which needs it to vanish. Removing them outright — what this did until
# 2026-09-17 — served only the second case and answered `not_monitored` to the
# first, which is a certainty the plugin had no right to state.
_SEPARATOR_CHARACTERS = str.maketrans("-._", "   ")
_REMOVED_SEPARATORS = str.maketrans("", "", "-._")

# An `http(s)://` scheme carries no identity a user, an alias, or a monitor's
# own display name would rely on to tell one service from another — it is
# noise every comparison should ignore, the same way a separator's exact
# spelling already is. Deliberately narrower than "any scheme": stripping only
# http/https keeps this predictable rather than guessing at what else might
# look like one. `www.` is left alone — unlike the scheme, it is part of the
# name an admin actually writes in the alias map.
_URL_SCHEME = re.compile(r"\bhttps?://", re.I)

# Unicode controls (newlines included), format characters (bidirectional
# overrides among them), lone surrogates, and line/paragraph separators. A
# character in one of these categories could forge what looks like a second,
# unrelated log line, or scramble how text displays. Shared by every boundary
# that neutralises external text — model input and monitor names alike —
# rather than each re-deriving its own notion of "unsafe".
_UNSAFE_TEXT_CATEGORIES = {"Cc", "Cf", "Cs", "Zl", "Zp"}

# A monitor id, as `/metrics` emits it, is a plain non-negative decimal
# integer. `int()` alone would also accept a leading sign and PEP-515
# underscore digit separators ("-12", "1_2"), silently turning a typo in
# `alias_map` into an id that can never match, or worse, one that matches an
# unrelated real monitor, with no warning either way.
_PLAIN_MONITOR_ID = re.compile(r"[0-9]+")

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

    The order is load-bearing: the scheme, if any, goes first — it shares no
    characters with the separators below, but stripping it before splitting
    on whitespace keeps the reasoning in one direction instead of two. The
    separators go next, because turning those in "U-GOV - Autenticazione"
    into spaces leaves runs of whitespace that the next pass then has to
    collapse.

    This is the word-separated form. `fuse_name()` builds the other one, and
    a comparison that uses only this one misses "UGOV" against "U-GOV".
    """
    text = _URL_SCHEME.sub("", str(value or ""))
    separated = text.translate(_SEPARATOR_CHARACTERS)
    return " ".join(separated.split()).casefold()


def fuse_name(value: str) -> str:
    """Fold a name with its separators removed rather than turned into spaces.

    "U-GOV - Autenticazione" becomes "ugov autenticazione", which is what lets
    a query of "UGOV" reach it, and "autenticazione UGOV" too — the words stay
    words, so the word-set comparison keeps working on them.

    This was `normalise_name()` until 2026-09-17, and it is kept as the second
    of two forms rather than replaced. Each one reaches a case the other
    cannot: this one joins an acronym split by hyphens, the other separates
    words joined by them. Comparing both is what makes "Portale-XX" and
    "Portale XX" the same service without losing "UGOV".

    The scheme is stripped here too, for the same reason as in
    `normalise_name()`: an alias written as `www.sns.it` must equal a request
    or a monitor name spelled `https://www.sns.it`, on both normalised forms,
    not just one of them.
    """
    text = _URL_SCHEME.sub("", str(value or ""))
    without_separators = text.translate(_REMOVED_SEPARATORS)
    return " ".join(without_separators.split()).casefold()


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

        # Squashed rather than merely normalised: an alias is matched exactly,
        # and "U-GOV", "Ugov" and "U GOV" have to be one key, not three. Since
        # 2026-09-17 `normalise_name()` keeps the separator as a space, so it
        # can no longer do that on its own.
        names = [_squash(part) for part in raw_aliases.split(",")]
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
            if not _PLAIN_MONITOR_ID.fullmatch(candidate):
                malformed_id = candidate
                break
            monitor_ids.append(int(candidate))

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

        # The alias itself is never refused for this — an over-long line is
        # still saved as written, matching every other tolerance in this
        # function — but past this many ids `describe_resolution()` always
        # answers `ambiguous` for it (see MAXIMUM_ALIAS_MATCHES), which is
        # otherwise silent and looks like a working alias that never resolves.
        if len(monitor_ids) > MAXIMUM_ALIAS_MATCHES:
            problems.append(
                f"line {number}: alias groups {len(monitor_ids)} ids, more "
                f"than the {MAXIMUM_ALIAS_MATCHES} ever reported together; "
                "it will always resolve as ambiguous"
            )

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
    this function must come from a real response. The status value is read as
    the float the exposition format says it is, and kept only when integral.
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
            # The exposition format defines a sample value as a float, so `1`
            # and `1.0` are the same metric and an instance is free to emit
            # either. Reading it as an int would skip every line of an instance
            # that emits the decimal form, leaving nothing to resolve against
            # and answering `unknown` to every question with no error to show
            # for it. `NaN` and `±Inf` parse as floats and are rejected here,
            # by the same test that rejects a non-integral value.
            value = float(match.group(2))
        except (KeyError, TypeError, ValueError):
            continue

        if not value.is_integer():
            continue
        status = int(value)

        if not _is_safe_monitor_name(monitor_name):
            continue

        names[monitor_id] = monitor_name
        statuses[monitor_id] = status

    return names, statuses


def _is_safe_monitor_name(value: str) -> bool:
    """Accept external monitor text only when it is safe to retain and log.

    Unicode controls and format characters include newlines, bidirectional
    overrides and invisible separators: a name carrying them could forge what
    looks like a second, unrelated log line, or change identity in a way that
    accidentally matches a different service. Dropping the whole metric is
    safer than silently rewriting it. Excessive length is rejected on the same
    ground. A URL-shaped name is not: Uptime Kuma itself defaults an HTTP(S)
    monitor's display name to the monitored URL until an admin renames it, so
    treating that shape as unsafe made an actively monitored, unrenamed
    service falsely report `not_monitored` — see `ISSUES_RESOLVED.md`,
    2026-09-23. Real monitor names, URL-shaped or not, already reach the log
    for an administrator by design; only the two structural risks above are
    filtered here.
    """
    if not value or len(value) > MAXIMUM_MONITOR_NAME_LENGTH:
        return False
    return not any(
        unicodedata.category(character) in _UNSAFE_TEXT_CATEGORIES
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

    # Both sides squashed, so the lookup stays a single exact dictionary hit
    # while tolerating a disagreement about whether a separator was a space or
    # a hyphen. Still exact: "Portale Demo" does not reach "Portale Demo Test".
    aliases = explicit_ids or {}
    alias_ids = aliases.get(_squash(requested_name))
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

    fused_wanted = fuse_name(requested_name)

    # Computed once per monitor and reused by both passes below, rather than
    # each recomputing it independently: a request that falls through to
    # containment — the expected path for a phrase-style query such as
    # "autenticazione UGOV" — would otherwise normalise every name twice.
    normalised_by_id = {
        monitor_id: (normalise_name(name), fuse_name(name))
        for monitor_id, name in names.items()
    }

    exact_matches = tuple(
        (monitor_id, names[monitor_id])
        for monitor_id, (normalised_name, fused_name) in normalised_by_id.items()
        if normalised_name == normalised_wanted or fused_name == fused_wanted
    )
    if exact_matches:
        return MonitorResolution(requested_name, exact_matches, False, ())

    containment_matches = tuple(
        (monitor_id, names[monitor_id])
        for monitor_id, (normalised_name, fused_name) in normalised_by_id.items()
        if _names_contain_each_other(normalised_wanted, normalised_name)
        or _names_contain_each_other(fused_wanted, fused_name)
    )
    return MonitorResolution(requested_name, containment_matches, False, ())


def _squash(value: str) -> str:
    """Fold a name to its spaceless form, for the exact-alias comparison only.

    The two sides of an alias lookup may disagree on whether a separator was a
    space or a hyphen and must still be one key. Deliberately not used for
    heuristic matching: with no word boundaries left it would match across
    words a reader would keep apart.
    """
    return normalise_name(value).replace(" ", "")


def _names_contain_each_other(left: str, right: str) -> bool:
    """Match normalised names by phrase containment or by their word sets.

    Plain substring containment requires both sides to be at least
    `MINIMUM_CONTAINMENT_LENGTH` characters — otherwise a short query would
    match by coincidence inside unrelated names, such as a monitor whose name
    happens to share one common short syllable with it.

    Called twice by `find_monitor()`, once on the word-separated form and once
    on the fused one. Both branches matter on both forms: the fused form is
    what carries "UGOV" and "autenticazione UGOV" against
    "U-GOV - Autenticazione", the separated one what carries "Portale-XX"
    against "Portale XX".
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

    Unsafe characters are replaced with a space before truncating: this text
    is untrusted (it comes from the model) and this is the one place every
    caller — the sentence builder and the adapter's diagnostic logging alike —
    goes through, so it is the one place a newline, a bidirectional override,
    or a line/paragraph separator can be stopped from forging a second,
    unrelated log line or scrambling how the sentence displays. The same
    category set `_is_safe_monitor_name()` uses for monitor names, applied
    here as a replacement rather than a rejection: unlike a monitor, a service
    name always needs some text to show, even truncated or partly blanked.
    """
    text = "".join(
        " " if unicodedata.category(character) in _UNSAFE_TEXT_CATEGORIES else character
        for character in str(value or "")
    )
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
    working service, and a request naming no service at all is `ambiguous`
    rather than a certainty about a service nobody named.
    """
    # A request that names nothing is not a search miss: `not_monitored` is a
    # certain answer, and there is nothing here to be certain about. The model
    # may legitimately call the tool with no argument, so this is a reachable
    # path and not a defensive check.
    if not normalise_name(resolution.requested_name):
        return (
            OUTCOME_AMBIGUOUS,
            "La richiesta non indica alcun servizio. Chiedi all'utente di "
            "quale servizio vuole conoscere lo stato.",
        )

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

    A request that names no service is reachable here too, when `/metrics`
    itself could not be read: the sentence then drops the interpolation rather
    than leaving a dangling "di .". The outcome is `unknown` either way, and the
    clause forbidding any conclusion is never varied.
    """
    requested = _truncate_service_name(service_name)
    subject = f"di {requested}" if normalise_name(requested) else "del servizio richiesto"
    return (
        f"Non è stato possibile determinare lo stato {subject}. "
        "Non trarre conclusioni: non affermare né che il servizio è attivo né "
        "che è guasto."
    )
