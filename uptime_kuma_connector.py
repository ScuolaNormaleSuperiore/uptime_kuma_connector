"""Uptime Kuma Connector: Cheshire Cat adapter.

The plugin loads, validates its settings, and exposes one tool that reads the
current status of a service. Its shape is:

    settings -> is_usable() -> httpx GET /metrics (basic auth, 2 s timeout)
             -> kuma_client.describe_status() -> sentence for the model

Every failure is caught and becomes the `unknown` outcome. No state is kept
between turns: the status is read when the question arrives. See
Specifiche.md, sections 2.2 and 2.5.
"""

from difflib import SequenceMatcher

import httpx
from cat.log import log
from cat.mad_hatter.decorators import plugin, tool

try:
    from . import kuma_client
    from .settings import SECURE_SCHEME, UptimeKumaConnectorSettings
except ImportError:  # pragma: no cover - depends on how the module is loaded
    import kuma_client
    from settings import SECURE_SCHEME, UptimeKumaConnectorSettings

# The call sits inside the turn, in front of a waiting user. A background job
# would allow ten seconds; here that would be ten seconds of silence in a
# conversation. See Specifiche.md, section 2.5.
REQUEST_TIMEOUT_SECONDS = 2.0


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
            "[uptime-kuma] could not read the stored settings "
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
            "[uptime-kuma] the stored settings are not valid "
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
    integration switched off. See Specifiche.md, section 5.
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


def _report_resolution_problems(payload: str, service_name: str, aliases: dict) -> None:
    """Log administrator-only diagnostics without adding them to the model text."""
    names, _statuses = kuma_client.parse_monitor_metrics(payload)
    resolution = kuma_client.find_monitor(names, service_name, aliases)

    if resolution.missing_alias_ids:
        missing = ", ".join(
            str(monitor_id) for monitor_id in resolution.missing_alias_ids
        )
        log.warning(
            "[uptime-kuma] alias "
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
            "[uptime-kuma] no monitor matches "
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
    signature = (settings.base_url, bool(settings.api_key), settings.alias_map)
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
        # real across a campus LAN. See Specifiche.md, section 2.3.
        log.warning(
            "[uptime-kuma] the instance URL is not HTTPS: the API key will "
            "travel in clear text on every call. Acceptable only if that "
            "traffic never leaves a private network."
        )

    if settings.base_url and not settings.api_key:
        # Half-configured is the state worth naming: somebody filled the URL and
        # stopped. Without this line the plugin is silently disabled and looks
        # configured in the panel.
        log.warning(
            "[uptime-kuma] the instance URL is set but the API key field is "
            "empty: the connector stays disabled and no call is made."
        )

    _aliases, problems = kuma_client.parse_alias_map(settings.alias_map)
    for problem in problems:
        log.warning(f"[uptime-kuma] alias map, {problem}")


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
        response = httpx.get(
            kuma_client.metrics_url(settings.base_url),
            headers={"Authorization": kuma_client.basic_auth_header(api_key)},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.text
        _outcome, sentence = kuma_client.describe_status(
            payload, service_name, aliases
        )
        _report_resolution_problems(payload, service_name, aliases)
        return sentence
    except Exception as failure:
        # The exception text may contain the URL, the Authorization header, or
        # an echoed credential. Its type gives operations enough to diagnose a
        # failure without taking that risk.
        log.warning(
            "[uptime-kuma] could not read the monitoring endpoint "
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
        "[uptime-kuma] plugin activated. The service_status tool reads "
        "Uptime Kuma on demand; no flow hook or cache is used."
    )
