"""Plugin settings, exposed in the Cheshire Cat admin panel.

**The model is deliberately empty.** The admin panel therefore shows a settings
tab with no fields, which is the honest rendering of a plugin that reads no
configuration because it does nothing.

The fields the specification calls for are listed below as a comment rather than
declared, so that adding one is a deliberate act and the empty form cannot be
mistaken for a rendering bug. See `DOC/Specifiche.md`, section 5.

Two constraints to respect when they arrive:

- **There is no field for the API key, and there must not be one.** It comes
  from `UPTIME_KUMA_API_KEY` in the environment. The core persists settings to
  `settings.json` in clear text, and this key grants read access to the whole
  monitoring system, so a panel field is not an acceptable fallback — it is the
  wrong path made convenient.
- **One short sentence per description.** On this admin panel a long title plus
  a long description makes the settings page scroll horizontally, past roughly
  200 characters for the pair.
"""

from pydantic import BaseModel

from cat.mad_hatter.decorators import plugin

# The fields to declare when the implementation starts:
#
#   base_url: str = ""
#       "Uptime Kuma: instance URL". Empty disables the connector.
#       Validator: must be an HTTPS URL, trailing slash stripped.
#
#       HTTPS is a constraint and not a preference. The API key travels in a
#       Basic Auth header, and base64 is an encoding rather than encryption, so
#       over plain HTTP the credential is transmitted in clear text. An instance
#       reachable only over HTTP needs a decision recorded in Specifiche.md,
#       not a quietly relaxed validator.
#
#   explicit_monitor_ids: str = ""
#       "Uptime Kuma: explicit monitor ids". Optional `name: id` pairs, for the
#       cases where the monitor name is not enough to resolve what a user typed.
#       Rendered as a text area, one pair per line.
#
# And nothing else. In particular no timeout field: 2 seconds is a property of
# sitting inside a conversation, not a preference, and no cache field: the
# status is read in real time, with no state kept between turns.


class UptimeKumaConnectorSettings(BaseModel):
    """No fields yet, on purpose. See the module docstring."""


@plugin
def settings_model():
    """Return the Pydantic model the admin panel builds its form from."""
    return UptimeKumaConnectorSettings
