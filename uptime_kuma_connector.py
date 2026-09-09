"""Uptime Kuma Connector: Cheshire Cat adapter.

The plugin loads, validates its settings, and exposes one tool that reads the
current status of a service. Its shape is:

    settings -> is_usable() -> bounded httpx stream of GET /metrics
             -> kuma_client.parse_monitor_metrics() -> kuma_client.find_monitor()
             -> kuma_client.describe_resolution() -> sentence for the model

The resolution from `find_monitor()` is kept around rather than discarded: the
adapter's own diagnostic logging needs the same one, and re-parsing `/metrics`
and re-resolving the request a second time just to log would cost that work
twice on every call.

Every failure is caught and becomes the `unknown` outcome. No state is kept
between turns: the status is read when the question arrives.
"""

from difflib import SequenceMatcher
from queue import Queue
from threading import Event, Thread

import httpx
from cat.log import log
from cat.mad_hatter.decorators import plugin, tool

if __package__:
    from . import kuma_client
    from .settings import SECURE_SCHEME, UptimeKumaConnectorSettings
else:  # pragma: no cover - used by the repository's top-level test import
    import kuma_client
    from settings import SECURE_SCHEME, UptimeKumaConnectorSettings

BYTES_PER_KIBIBYTE = 1024


class MetricsResponseTooLarge(Exception):
    """The endpoint exceeded the configured in-memory response ceiling."""


class MetricsRequestDeadlineExceeded(Exception):
    """The complete request did not finish within its configured deadline."""


def _read_metrics_stream(
    url: str,
    authorization: str,
    timeout_seconds: float,
    maximum_bytes: int,
    cancelled: Event,
) -> str:
    """Read one response incrementally, stopping before it exceeds its limit."""
    with httpx.stream(
        "GET",
        url,
        headers={"Authorization": authorization},
        timeout=timeout_seconds,
    ) as response:
        response.raise_for_status()

        declared_size = response.headers.get("content-length")
        if declared_size is not None:
            try:
                if int(declared_size) > maximum_bytes:
                    raise MetricsResponseTooLarge
            except ValueError:
                # A malformed header is not trusted; the measured byte limit
                # below remains authoritative.
                pass

        payload = bytearray()
        for chunk in response.iter_bytes():
            if cancelled.is_set():
                raise MetricsRequestDeadlineExceeded
            if len(payload) + len(chunk) > maximum_bytes:
                raise MetricsResponseTooLarge
            payload.extend(chunk)

    return payload.decode("utf-8")


def _fetch_metrics(
    settings: UptimeKumaConnectorSettings, authorization: str
) -> str:
    """Fetch `/metrics` with both a byte ceiling and a total wall-clock limit.

    httpx's timeout bounds individual socket operations, not the complete
    response. The daemon worker lets the calling conversation stop at the
    configured total deadline as well. Cancellation is observed between chunks;
    an in-progress socket operation remains bounded by the same httpx timeout.
    """
    result: Queue = Queue(maxsize=1)
    cancelled = Event()

    def fetch() -> None:
        try:
            payload = _read_metrics_stream(
                kuma_client.metrics_url(settings.base_url),
                authorization,
                settings.request_timeout_seconds,
                settings.maximum_response_size_kib * BYTES_PER_KIBIBYTE,
                cancelled,
            )
            result.put((True, payload))
        except Exception as failure:
            result.put((False, failure))

    worker = Thread(target=fetch, daemon=True, name="uptime-kuma-metrics")
    worker.start()
    worker.join(settings.request_timeout_seconds)
    if worker.is_alive():
        cancelled.set()
        raise MetricsRequestDeadlineExceeded

    succeeded, value = result.get_nowait()
    if not succeeded:
        raise value
    return value


def resolve_api_key(settings: UptimeKumaConnectorSettings) -> str:
    """The API key, from the admin panel and from nowhere else.

    One source, deliberately. A field plus an environment fallback would be two
    places that can disagree, and the question "which one is this instance
    actually using?" has no answer visible in the panel — the failure this
    plugin already refuses to build in `is_usable()`.

    The value is stripped by the settings validator, so this is a read with no
    logic to get wrong. **Its result must never reach a log line.**
    """
    return settings.api_key


def load_settings(cat) -> UptimeKumaConnectorSettings:
    """Read and validate the settings, **never raising**.

    A configuration problem must not take the turn down, so every failure ends
    in the model defaults, which are a working "not configured" state: no
    instance URL, connector disabled, no network call attempted.

    Validating through the model rather than reading the raw dictionary is what
    makes a new setting work immediately on an existing installation: the model
    supplies the default of any absent field. The field shows up empty in the
    admin form until the form is saved once, which is a rendering detail rather
    than a missing value.
    """
    try:
        stored = cat.mad_hatter.get_plugin().load_settings()
    except Exception as failure:  # the core, the file, or the plugin lookup
        log.warning(
            "[uptime_kuma_connector] could not read the stored settings "
            f"({type(failure).__name__}); the connector stays disabled."
        )
        return UptimeKumaConnectorSettings()

    try:
        settings = UptimeKumaConnectorSettings(**(stored or {}))
    except Exception as failure:
        # Reachable when settings.json was edited by hand or written by an
        # older version of the model: the strict base_url validator refuses it
        # on read as it would on save. Disabling the connector is the safe
        # answer; raising here would break a conversation over a typo.
        log.warning(
            "[uptime_kuma_connector] the stored settings are not valid "
            f"({type(failure).__name__}); the connector stays disabled."
        )
        return UptimeKumaConnectorSettings()

    report_configuration_problems(settings)
    return settings


def is_usable(settings: UptimeKumaConnectorSettings) -> bool:
    """The single point that decides whether the connector can be used.

    Answers on the instance URL **and** the API key together, and every path
    must go through it — including any future switch, which belongs inside this
    function rather than beside it.

    One function rather than two checks scattered around, because in an
    analogous integration already in production it happened twice that one place
    tested only the identifier and showed a monitoring indicator with the
    integration switched off.
    """
    return bool(settings.base_url) and bool(resolve_api_key(settings))


def alias_map(settings: UptimeKumaConnectorSettings) -> dict:
    """The configured aliases, as `{normalised alias: monitor ids}`.

    The parsing lives in `kuma_client` because it is pure logic; this wrapper
    exists so callers never see the problem list they are not going to log.
    """
    aliases, _problems = kuma_client.parse_alias_map(settings.alias_map)
    return aliases


def _closest_monitor_names(service_name: str, names: tuple[str, ...]) -> tuple[str, ...]:
    """Return at most three names useful to an administrator diagnosing a miss."""
    wanted = kuma_client.normalise_name(service_name)
    ranked = sorted(
        names,
        key=lambda name: SequenceMatcher(
            None, wanted, kuma_client.normalise_name(name)
        ).ratio(),
        reverse=True,
    )
    return tuple(ranked[:3])


def _report_resolution_problems(
    resolution: kuma_client.MonitorResolution, names: dict
) -> None:
    """Log administrator-only diagnostics without adding them to the model text.

    Takes the `MonitorResolution` and the parsed monitor names as already
    computed by the caller, rather than the raw payload: `service_status()`
    needs the same resolution to build the sentence, and re-parsing `/metrics`
    and re-resolving the request here just to log would cost that work twice
    on every single call.
    """
    if resolution.missing_alias_ids:
        missing = ", ".join(
            str(monitor_id) for monitor_id in resolution.missing_alias_ids
        )
        log.warning(
            "[uptime_kuma_connector] alias "
            f"'{resolution.requested_name}' refers to monitor ids absent from "
            f"/metrics: {missing}."
        )

    if not resolution.matches and not resolution.used_alias:
        closest = _closest_monitor_names(
            resolution.requested_name,
            tuple(kuma_client.monitored_service_names(names)),
        )
        suffix = f" Closest monitor names: {', '.join(closest)}." if closest else ""
        log.warning(
            "[uptime_kuma_connector] no monitor matches "
            f"'{resolution.requested_name}'.{suffix}"
        )


# Remembers what was already reported, so a problem is logged when it appears
# rather than on every turn. Module state resets when the core reloads the
# plugin, which is the moment a fresh report is wanted anyway.
_reported_configuration = None


def report_configuration_problems(settings: UptimeKumaConnectorSettings) -> None:
    """Log what is wrong with the configuration, once per change.

    This is the only channel an administrator has. The panel cannot show a
    problem found while reading — the alias map is accepted on save on purpose,
    so that one bad line does not disable every other service — and the
    sentences that reach the model never carry configuration detail, because
    they are text a user may read.

    Logging on every turn would drown it, so the report is memoised on the
    values it describes: saving the form produces a fresh report on the next
    turn, and an unchanged configuration stays quiet.
    """
    global _reported_configuration

    # `bool(...)` and not the key itself: this module-level variable outlives
    # the turn, and a credential kept in it is a credential in every memory dump
    # and every debugger session for as long as the plugin stays loaded.
    signature = (
        settings.base_url,
        bool(settings.api_key),
        settings.alias_map,
        settings.maximum_response_size_kib,
        settings.request_timeout_seconds,
    )
    if signature == _reported_configuration:
        return
    _reported_configuration = signature

    if settings.base_url and not settings.base_url.lower().startswith(
        f"{SECURE_SCHEME}:"
    ):
        # Accepted, but never silently: the API key travels as
        # `base64(":" + key)` in a Basic Auth header, which is an encoding and
        # not encryption, so on plain HTTP the credential is readable by
        # anything that sees the traffic. Negligible on a container network,
        # real across a campus LAN.
        log.warning(
            "[uptime_kuma_connector] the instance URL is not HTTPS: the API key will "
            "travel in clear text on every call. Acceptable only if that "
            "traffic never leaves a private network."
        )

    if settings.base_url and not settings.api_key:
        # Half-configured is the state worth naming: somebody filled the URL and
        # stopped. Without this line the plugin is silently disabled and looks
        # configured in the panel.
        log.warning(
            "[uptime_kuma_connector] the instance URL is set but the API key field is "
            "empty: the connector stays disabled and no call is made."
        )

    _aliases, problems = kuma_client.parse_alias_map(settings.alias_map)
    for problem in problems:
        log.warning(f"[uptime_kuma_connector] alias map, {problem}")


@tool(return_direct=False)
def service_status(service_name: str, cat) -> str:
    """Verifica se un servizio è attivo quando l'utente segnala un problema.

    Usalo per domande come «la VPN non funziona», «non riesco ad autenticarmi su
    U-GOV», «non riesco ad accedere a Esse3», «il portale è giù?» o «è un
    problema mio o del servizio?». Passa il nome del servizio chiesto dall'utente.
    """
    try:
        settings = load_settings(cat)
        if not is_usable(settings):
            return kuma_client.unreachable_sentence(service_name)

        api_key = resolve_api_key(settings)
        aliases = alias_map(settings)
        log.info("[uptime_kuma_connector] requesting current monitor status.")
        payload = _fetch_metrics(
            settings,
            kuma_client.basic_auth_header(api_key),
        )
        names, statuses = kuma_client.parse_monitor_metrics(payload)
        resolution = kuma_client.find_monitor(names, service_name, aliases)
        if names:
            outcome, sentence = kuma_client.describe_resolution(resolution, statuses)
        else:
            outcome = kuma_client.OUTCOME_UNKNOWN
            sentence = kuma_client.unreachable_sentence(service_name)
        log.info(
            "[uptime_kuma_connector] monitoring endpoint request succeeded "
            f"(outcome: {outcome})."
        )
        _report_resolution_problems(resolution, names)
        return sentence
    except Exception as failure:
        # The exception text may contain the URL, the Authorization header, or
        # an echoed credential. Its type gives operations enough to diagnose a
        # failure without taking that risk.
        log.warning(
            "[uptime_kuma_connector] could not read the monitoring endpoint "
            f"({type(failure).__name__}); the status is unknown."
        )
        return kuma_client.unreachable_sentence(service_name)


@plugin
def activated(plugin):
    """Announce that the plugin loaded.

    `activated` rather than `after_cat_bootstrap`, and the two are not
    interchangeable: `after_cat_bootstrap` runs once when the core starts, so it
    says nothing about a plugin switched on later from the admin panel — which
    is exactly when the code on disk is re-read and a load failure happens.

    It reads no settings: `activated` receives the plugin rather than the `cat`
    object, and a network call or a settings read here would put work in front
    of an administrator clicking a switch. The configuration is reported on the
    first turn that reads it instead.
    """
    log.info(
        "[uptime_kuma_connector] plugin activated. The service_status tool reads "
        "Uptime Kuma on demand; no flow hook or cache is used."
    )
