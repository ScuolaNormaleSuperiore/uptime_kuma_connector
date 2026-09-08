"""Pure logic for reading Uptime Kuma. Imports nothing from `cat`, does no I/O.

**Nothing here is implemented.** Every function is a signature with a neutral
body, kept so the shape of the module matches the specification and so the next
person starts from a map rather than a blank file. The specification is
`DOC/Specifiche.md`; the section numbers below point into it.

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

# Removed before comparing names, so "UGOV" finds "U-GOV - Autenticazione" on
# its own and a whole class of aliases never has to be written by hand.
_INSIGNIFICANT_CHARACTERS = str.maketrans("", "", "-._")


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

    Not implemented. See Specifiche.md, section 2.1: `/metrics` is preferred
    over the status-page endpoints because one line carries name, id and status
    together, and because the alternative requires a *published* status page.
    """
    return ""


def basic_auth_header(api_key: str) -> str:
    """The Authorization header value for `/metrics`.

    Not implemented. The form is a convention of Uptime Kuma and is not
    obvious: basic auth with an **empty username** and the API key as the
    password, `base64(":" + api_key)`. See Specifiche.md, section 2.3.
    """
    return ""


def parse_monitor_metrics(payload: str) -> tuple[dict[int, str], dict[int, int]]:
    """Read `monitor_status` lines into names and statuses, both keyed by id.

    Not implemented. Returns `(names, statuses)`.

    The lines look like:

        monitor_status{monitor_id="12",monitor_name="VPN",monitor_type="http"} 1

    A malformed line or a missing label is skipped, never raised on. The label
    format is the part most easily assumed wrong, which is why the fixtures for
    this function must come from a real response. See Specifiche.md, 3.1 and 6.
    """
    return {}, {}


def find_monitor(
    names: Mapping[int, str],
    wanted: str,
    explicit_ids: Mapping[str, int] | None = None,
) -> tuple[int, str] | None:
    """Resolve what the user typed to exactly one monitor.

    Not implemented. Returns `(id, name)` or `None`.

    Order, per Specifiche.md 3.2: an explicitly configured id wins; then a
    case-insensitive exact name match; then a containment match. **An ambiguous
    query returns `None`**, deliberately — naming one of several monitors would
    present a guess to the user as a fact.
    """
    return None


def monitored_service_names(names: Mapping[int, str]) -> Sequence[str]:
    """Every monitor name, so a miss can say what *is* monitored.

    Not implemented. This is not a convenience: without it a configuration
    mistake is silent, because a wrong name produces the same answer as a
    service that genuinely is not monitored. See Specifiche.md, section 3.3.
    """
    return ()


def describe_status(
    payload: str,
    service_name: str,
    explicit_ids: Mapping[str, int] | None = None,
) -> tuple[str, str]:
    """Turn a `/metrics` response into one sentence for the model.

    Not implemented. Returns `(outcome, sentence)`, where the outcome is one of
    the three constants above and the sentence is what reaches the model.

    The sentence is product text, not debug output: it is in Italian, it names
    the monitor as Uptime Kuma names it, and it never carries a URL, a key or an
    internal identifier. See Specifiche.md, section 4.
    """
    return OUTCOME_UNKNOWN, ""


def unreachable_sentence() -> str:
    """What to tell the model when `/metrics` could not be read at all.

    Not implemented. It must state that the status is not verifiable **and**
    instruct the model not to conclude anything: asked "is the VPN down?", a
    model fills a silence if it is allowed to. This is the invariant of the
    whole plugin — never invent a state. See Specifiche.md, section 4.
    """
    return ""
