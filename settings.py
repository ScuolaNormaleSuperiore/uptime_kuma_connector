"""Plugin settings, exposed in the Cheshire Cat admin panel.

Three fields, and the absences matter as much as the fields.

- **The API key is a panel field, and the panel is the only place it lives.**
  Decided on 2026-09-08, reversing the earlier environment-only rule: everything
  needed to reach Uptime Kuma is configured in one place, by whoever administers
  the instance, without touching the deployment.

  The cost is real and is not mitigated by anything in this file. The core
  persists settings to `settings.json` in clear text under the plugin folder, so
  the key reaches every backup, container snapshot and support copy that folder
  appears in, and the panel renders it as ordinary text. `settings.json` is in
  `.gitignore`, which keeps it out of the repository and nowhere else. Whoever
  can read the plugin folder can read the key, and that key grants read access
  to the whole monitoring system — so it must be a **read-only** key, and
  rotating it is the only remedy once the folder has been copied.
- **No timeout field.** Two seconds is a property of sitting inside a
  conversation in front of a waiting user, not a preference to tune.
- **No cache field.** The status is read in real time; there is no state kept
  between turns.
- **No general on/off switch.** An empty instance URL disables the connector,
  and Cheshire Cat already has its own switch — deactivating the plugin. A
  second one would be a field that can disagree with the first.

The titles and descriptions are in **Italian**, because service owners read the
panel. The code and comments stay in English. One short sentence per description:
on this panel a long title plus a long description makes the settings page scroll
horizontally, past roughly 200 characters for the pair.

The validators are deliberately asymmetric, and the asymmetry is the point.
`base_url` is **strict**, so a typo is refused while the person who made it is
still looking at the form. `alias_map` **never refuses**, so one bad line cannot
disable the resolution of every other service. `api_key` only strips, because
Uptime Kuma promises nothing about the shape of a key and a wrong one is told
apart from a right one by the instance answering 401, not by a pattern here.
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
# again whenever that configuration changes.
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

    # Read-only key generated in Uptime Kuma, under Settings -> API Keys. It
    # authenticates `/metrics` as basic auth with an **empty username** and the
    # key as the password, `base64(":" + key)` — a convention of Uptime Kuma
    # and not an obvious one.
    #
    # Empty disables the connector just as an empty URL does: both are read by
    # `is_usable()` in the adapter, which is the single point that decides.
    #
    # No `json_schema_extra` marker asks the panel to mask it. None of the
    # plugins on this instance uses one, so nothing establishes that the panel
    # supports it, and a marker the panel does not know fails **silently** —
    # see the note on `TEXT_AREA` above. A masked field that is not masked is
    # worse than a field nobody believed was protected.
    api_key: str = Field(
        default="",
        title="Uptime Kuma: API key",
        description="Chiave di sola lettura, da Settings > API Keys di Uptime Kuma.",
    )

    # A multi-line box, because the format is one entry per line and an
    # installation with a dozen services would otherwise be edited through a
    # single-line input scrolling sideways.
    alias_map: str = Field(
        default="",
        title="Uptime Kuma: mappa alias",
        description="Opzionale, una voce per riga: nome, altro nome: id, id (max 10 id per alias)",
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

        # Credentials in the URL are refused rather than silently stripped, so
        # the person who pasted them knows they did. The key belongs in its own
        # field, where the adapter reads it and where it is not also part of
        # every log line that happens to carry the instance URL.
        if parts.username or parts.password:
            raise ValueError(
                "L'URL non deve contenere credenziali: usare il campo "
                "API key"
            )

        if parts.query or parts.fragment:
            raise ValueError(
                "L'URL deve essere solo quello dell'istanza, senza parametri "
                "di query né frammenti"
            )

        # A path is kept: an instance behind a reverse proxy can legitimately
        # live under one, for example https://intranet.example.org/kuma
        return text

    @field_validator("api_key")
    @classmethod
    def normalise_api_key(cls, value: str) -> str:
        """Strip the key, and never refuse it.

        Stripping is not cosmetic: a key pasted from a dashboard arrives with a
        trailing newline often enough, and that byte would travel inside the
        Basic Auth header and turn every call into a 401 that looks like a
        wrong key rather than a stray character.

        Nothing else is validated. Uptime Kuma documents neither the length nor
        the character set of its keys as a contract, so any rule here would be
        a guess that starts refusing valid keys the day the format changes. A
        wrong key is caught by the instance answering 401, which is where it
        can actually be told apart from a right one.

        **Never raise, and never log the value.** An empty result is a working
        state: it disables the connector.
        """
        return (value or "").strip()

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
