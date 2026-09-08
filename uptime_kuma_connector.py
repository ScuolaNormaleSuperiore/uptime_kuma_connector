"""Uptime Kuma Connector: Cheshire Cat adapter.

**The configuration is wired; the behaviour is not.** The plugin loads, exposes
the two settings the specification calls for, reads and validates them, decides
whether the connector is usable, and reports configuration problems in the log.
It still makes no request to Uptime Kuma and answers no question. The
specification is `DOC/Specifiche.md`.

What is deliberately absent, and must stay absent until it is implemented:

- **no `@tool`**, so nothing can invoke the empty decision logic by accident
- **no flow hook**, so no conversation is touched
- **no network call**, so no timeout and no failure path exists yet

Only the activation announcement is registered. It is a diagnostic rather than a
feature, and it is the one thing no per-turn line can report: a plugin that
fails to load registers nothing, so silence is what a broken activation shares
with a plugin that simply has nothing to say. Right now it says out loud that
nothing is implemented, which is the honest message.

When the implementation starts, the shape is:

    settings -> is_usable() -> httpx GET /metrics (basic auth, 2 s timeout)
             -> kuma_client.describe_status() -> sentence for the model

with every failure caught and turned into the `unknown` outcome, and with no
state kept between turns: the status is read when the question arrives. See
Specifiche.md, sections 2.2 and 2.5.
"""

import os

from cat.log import log
from cat.mad_hatter.decorators import plugin

try:
    from . import kuma_client
    from .settings import SECURE_SCHEME, UptimeKumaConnectorSettings
except ImportError:  # pragma: no cover - depends on how the module is loaded
    import kuma_client
    from settings import SECURE_SCHEME, UptimeKumaConnectorSettings

# Read from the environment and never from the admin panel: the core persists
# settings to settings.json in clear text, and this key grants read access to
# the whole monitoring system. See Specifiche.md, section 2.3.
API_KEY_ENVIRONMENT_VARIABLE = "UPTIME_KUMA_API_KEY"

# The call sits inside the turn, in front of a waiting user. A background job
# would allow ten seconds; here that would be ten seconds of silence in a
# conversation. See Specifiche.md, section 2.5.
REQUEST_TIMEOUT_SECONDS = 2.0


def resolve_api_key() -> str:
    """The API key, from the environment only.

    Not implemented beyond the read itself, which has no logic to get wrong.
    There is deliberately no settings field to fall back to.
    """
    return os.environ.get(API_KEY_ENVIRONMENT_VARIABLE, "").strip()


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
    return bool(settings.base_url) and bool(resolve_api_key())


def alias_map(settings: UptimeKumaConnectorSettings) -> dict:
    """The configured aliases, as `{normalised alias: monitor ids}`.

    The parsing lives in `kuma_client` because it is pure logic; this wrapper
    exists so callers never see the problem list they are not going to log.
    """
    aliases, _problems = kuma_client.parse_alias_map(settings.alias_map)
    return aliases


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

    signature = (settings.base_url, settings.alias_map)
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

    _aliases, problems = kuma_client.parse_alias_map(settings.alias_map)
    for problem in problems:
        log.warning(f"[uptime-kuma] alias map, {problem}")


def service_status(service_name, cat):
    """Report whether a monitored service is currently up.

    Not implemented, and **not registered as a tool**: it is a plain function so
    that nothing can invoke it while it decides nothing.

    When it becomes a tool it takes `return_direct=False`, because it does not
    answer the user — it hands a fact to the model, which merges it with the
    procedure retrieved from the knowledge base. Its docstring is what the model
    reads to decide whether to call it, so it will have to be written in the
    language the users write in.
    """
    return ""


@plugin
def activated(plugin):
    """Announce that the plugin loaded, and how far the implementation got.

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
        "[uptime-kuma] plugin activated. Settings are wired and validated; no "
        "behaviour is implemented yet: no tool is registered, no hook is "
        "registered, and no request is made to Uptime Kuma. See "
        "DOC/Specifiche.md for the specification to build."
    )
