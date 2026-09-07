"""Uptime Kuma Connector: Cheshire Cat adapter.

**Nothing is wired.** The plugin loads, exposes an empty settings tab, and does
nothing else. The specification to rebuild it from is `DOC/Specifiche.md`.

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
    from .settings import UptimeKumaConnectorSettings
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from settings import UptimeKumaConnectorSettings

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
    """Read and validate the settings, never raising.

    Not implemented. When it is: a configuration problem must not take the turn
    down, so this catches everything and falls back to the model defaults, which
    are a working "not configured" state.
    """
    return UptimeKumaConnectorSettings()


def is_usable(settings: UptimeKumaConnectorSettings) -> bool:
    """The single point that decides whether the connector can be used.

    Not implemented. It must answer on the instance URL **and** the API key
    together, and every path must go through it.

    One function rather than two checks scattered around, because in an
    analogous integration already in production it happened twice that one place
    tested only the identifier and showed a monitoring indicator with the
    integration switched off. See Specifiche.md, section 5.
    """
    return False


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
    """Announce that the plugin loaded, and that it does nothing yet.

    `activated` rather than `after_cat_bootstrap`, and the two are not
    interchangeable: `after_cat_bootstrap` runs once when the core starts, so it
    says nothing about a plugin switched on later from the admin panel — which
    is exactly when the code on disk is re-read and a load failure happens.
    """
    log.info(
        "[uptime-kuma] plugin activated. No functionality is implemented: no "
        "tool is registered, no hook is registered, and no request is made to "
        "Uptime Kuma. See DOC/Specifiche.md for the specification to build."
    )
