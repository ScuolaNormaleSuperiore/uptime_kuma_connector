"""Plugin settings, exposed in the Cheshire Cat admin panel.

Two fields, and the absences matter as much as the fields. See
`DOC/Specifiche.md`, section 5.

- **There is no field for the API key, and there must not be one.** It comes
  from `UPTIME_KUMA_API_KEY` in the environment. The core persists settings to
  `settings.json` in clear text, and this key grants read access to the whole
  monitoring system, so a panel field is not an acceptable fallback — it is the
  wrong path made convenient. A test asserts that no such field exists.
- **No timeout field.** Two seconds is a property of sitting inside a
  conversation in front of a waiting user, not a preference to tune.
- **No cache field.** The status is read in real time; there is no state kept
  between turns.
- **No general on/off switch.** An empty instance URL disables the connector,
  and Cheshire Cat already has its own switch — deactivating the plugin. A
  second one would be a field that can disagree with the first.

The titles and descriptions are in **Italian**, because the panel is read by the
same service owners who read `DOC/Specifiche.md`. The code and the comments stay
in English. One short sentence per description: on this panel a long title plus
a long description makes the settings page scroll horizontally, past roughly 200
characters for the pair.

The two validators are deliberately asymmetric, and the asymmetry is the point.
`base_url` is **strict**, so a typo is refused while the person who made it is
still looking at the form. `alias_map` **never refuses**, so one bad line cannot
disable the resolution of every other service.
"""

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from cat.mad_hatter.decorators import plugin

# HTTP is accepted, and that is a decision rather than an oversight. The API key
# travels in a Basic Auth header as `base64(":" + key)`, and base64 is an
# encoding rather than encryption, so over plain HTTP the credential is readable
# by anything that sees the traffic. It is accepted because the target
# deployment may reach Uptime Kuma over a private network — a container network
# on the same host makes the exposure negligible — and because refusing it would
# only push the URL into a place nobody validates.
#
# What must not happen is that the choice becomes invisible: the adapter logs a
# warning the first time it reads a configuration whose URL is not HTTPS, and
# again whenever that configuration changes. See `DOC/Specifiche.md`, 2.3.
ACCEPTED_SCHEMES = ("https", "http")

SECURE_SCHEME = "https"

# How the admin panel is asked to render a field as a multi-line box.
#
# The nesting is the whole point, and getting it wrong fails silently. The panel
# reads `extra.type` from the published JSON Schema, so the marker has to sit in
# a nested `extra` object while `type` keeps its real value, `string`. Writing
# `json_schema_extra={"type": "TextArea"}` instead **replaces** `type`, which
# publishes something that is not a valid JSON Schema type and puts the marker
# where the panel never looks: the field renders as a single-line input and
# nothing anywhere reports a problem.
#
# Taken from the sibling `rag-guardrails` plugin, which found this the hard way.
# `Field(extra={...})` produces the same schema and is what the older plugins on
# this instance use, but it is a deprecated Pydantic v2 spelling.
TEXT_AREA = {"extra": {"type": "TextArea"}}


class UptimeKumaConnectorSettings(BaseModel):
    """The form the admin panel builds, and the shipped defaults.

    Both defaults are empty, which is a working "not configured" state: with no
    instance URL the connector is disabled and no network call is attempted.
    That is the correct shipped default for a plugin that talks to somebody
    else's monitoring system.
    """

    base_url: str = Field(
        default="",
        title="Uptime Kuma: URL istanza",
        description="Vuoto disabilita il connettore. Preferire HTTPS.",
    )

    # A multi-line box, because the format is one entry per line and an
    # installation with a dozen services would otherwise be edited through a
    # single-line input scrolling sideways.
    alias_map: str = Field(
        default="",
        title="Uptime Kuma: mappa alias",
        description="Opzionale, una voce per riga: nome, altro nome: id, id",
        json_schema_extra=TEXT_AREA,
    )

    @field_validator("base_url")
    @classmethod
    def normalise_base_url(cls, value: str) -> str:
        """Normalise the instance URL, or refuse it with a readable message.

        Strict on purpose: the panel shows the error, and that is the cheapest
        moment for whoever typed it to fix it. `load_settings()` in the adapter
        is the indulgent half — it catches a value that got into
        `settings.json` some other way and falls back to the defaults, so a bad
        stored value disables the connector instead of breaking a turn.
        """
        text = (value or "").strip()
        if not text:
            return ""

        # A trailing slash is the banality that costs an afternoon: joined with
        # `/metrics` it produces `//metrics`, and some reverse proxies answer
        # 404 rather than normalising it.
        text = text.rstrip("/")

        parts = urlsplit(text)

        if not parts.scheme:
            raise ValueError(
                "Manca lo schema: l'URL deve iniziare con https:// oppure http://"
            )

        if parts.scheme.lower() not in ACCEPTED_SCHEMES:
            raise ValueError(
                f"Schema non supportato: '{parts.scheme}'. Sono ammessi solo "
                "https:// e http://"
            )

        if not parts.hostname:
            raise ValueError("Manca l'indirizzo dell'istanza dopo lo schema")

        # Credentials in the URL would be written to settings.json in clear
        # text, which is the exact exposure the missing API key field exists to
        # avoid. Refused rather than silently stripped, so the person who pasted
        # them knows they did.
        if parts.username or parts.password:
            raise ValueError(
                "L'URL non deve contenere credenziali: usare la variabile "
                "d'ambiente UPTIME_KUMA_API_KEY"
            )

        if parts.query or parts.fragment:
            raise ValueError(
                "L'URL deve essere solo quello dell'istanza, senza parametri "
                "di query né frammenti"
            )

        # A path is kept: an instance behind a reverse proxy can legitimately
        # live under one, for example https://intranet.example.org/kuma
        return text

    @field_validator("alias_map")
    @classmethod
    def normalise_alias_map(cls, value: str) -> str:
        """Tidy the alias map without ever refusing it.

        **This validator must never raise.** A malformed line is discarded on
        its own, with a log line naming it, by `kuma_client.parse_alias_map()`;
        every other line survives.

        Refusing the whole field because line 7 has a non-numeric id would be
        the `requirements.txt` defect moved into the user interface: the core
        reads that file by calling `packaging.Requirement()` in a loop inside a
        `try` that abandons the entire list on the first bad line, installing
        nothing. Here the same mistake would disable the resolution of every
        service because of one stray character.

        The cost, declared: whoever saves gets no feedback about the broken
        line and has to read the log. That is the price of having no global
        failure point.
        """
        text = str(value or "")
        return text.replace("\r\n", "\n").replace("\r", "\n").strip()


@plugin
def settings_model():
    """Return the Pydantic model the admin panel builds its form from."""
    return UptimeKumaConnectorSettings
