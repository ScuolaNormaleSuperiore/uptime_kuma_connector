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

# The three outcomes the module can return. `unknown` is not an error state: it
# is the honest answer when Uptime Kuma cannot be read or does not know the
# service, and keeping it distinct from `down` is the point of the whole design.
# See Specifiche.md, section 4.
OUTCOME_KNOWN = "known"
OUTCOME_NOT_MONITORED = "not_monitored"
OUTCOME_UNKNOWN = "unknown"


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
