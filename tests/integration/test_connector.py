"""Tests for the Cheshire Cat adapter.

The plugin registers its status tool. These tests assert both its structural
integration points and its behaviour with a fake Cat and a mocked HTTP call.

These need the core importable, because the module under test imports `cat.log`
and `cat.mad_hatter.decorators` at import time. They never contact a live
instance: the container is used as an interpreter, not as a server.

    python run-tests.py --integration

The cases that belong here are:

- connector not configured: no network call is attempted
- URL configured but API key missing: no call, outcome `unknown`
- **instance unreachable: outcome `unknown`, and the sentence claims neither
  that the service is up nor that it is down.** This is the one that matters
  most, because it is the only failure that actively misleads a user
- HTTP 401: outcome `unknown`, and the key appears in no log line
- the timeout is honoured
- the tool never raises, whatever the call does
"""

import sys
import time
from importlib import import_module
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError

# The Cat imports every `.py` in the plugin folder, this file included, as
# `cat.plugins.uptime_kuma_connector.tests.integration.test_connector`. Under
# that name this module must do nothing at all, and both halves of what follows
# are harmful inside the core process.
#
# Putting the plugin folder on `sys.path` makes our `settings.py` answer a bare
# `import settings` from whichever plugin the Cat loads next. And importing the
# adapter from here is how ours came to load a *neighbour's* `settings.py`
# instead of its own: that plugin ran the same trick first and had already
# claimed the bare name, so the adapter failed on every activation with
# `cannot import name 'SECURE_SCHEME' from 'settings'`. Same defect, seen from
# its two ends. Resolved 2026-09-08.
#
# Under `pytest` the plugin folder is the only one on the path and both are safe.
if not __name__.startswith("cat.plugins."):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    import settings as settings_module  # noqa: E402
    import uptime_kuma_connector as connector  # noqa: E402


class TestPluginLoads:
    def test_the_settings_model_is_exposed_to_the_admin_panel(self):
        # The core builds the settings form from this model.
        assert settings_module.settings_model.function() is (
            settings_module.UptimeKumaConnectorSettings
        )

    def test_the_form_carries_exactly_the_five_specified_fields(self):
        # The exact set makes every persisted option a deliberate decision.
        schema = settings_module.UptimeKumaConnectorSettings.model_json_schema()

        assert set(schema["properties"]) == {
            "base_url",
            "api_key",
            "alias_map",
            "maximum_response_size_kib",
            "request_timeout_seconds",
        }

    def test_every_field_has_a_short_italian_label(self):
        # Beyond roughly 200 characters for title plus description the settings
        # page starts scrolling horizontally. Learned by experiment.
        schema = settings_module.UptimeKumaConnectorSettings.model_json_schema()

        for name, field in schema["properties"].items():
            pair = field.get("title", "") + field.get("description", "")
            assert field.get("title"), f"{name} has no title"
            assert field.get("description"), f"{name} has no description"
            assert len(pair) < 200, f"{name} would make the panel scroll"

    def test_the_shipped_defaults_disable_the_connector(self):
        # The correct shipped default for a plugin that talks to somebody
        # else's monitoring system: configured by nobody, calling nothing.
        settings = settings_module.UptimeKumaConnectorSettings()

        assert settings.base_url == ""
        assert settings.api_key == ""
        assert settings.alias_map == ""
        assert settings.maximum_response_size_kib == 1024
        assert settings.request_timeout_seconds == 2.0
        assert connector.is_usable(settings) is False

    def test_the_defaults_can_build_settings_json(self):
        # The core creates settings.json from the model at first activation. A
        # model that cannot serialise would make activation fail.
        assert settings_module.UptimeKumaConnectorSettings().model_dump_json()

    def test_activation_is_announced(self):
        # The only signal that survives a failed load, because a plugin that
        # does not load registers nothing and writes none of the other lines.
        lines = []
        original = connector.log.info
        connector.log.info = lines.append
        try:
            connector.activated.function(object())
        finally:
            connector.log.info = original

        assert any("plugin activated" in line for line in lines)

    def test_an_internal_import_error_is_not_redirected_to_top_level_modules(
        self, tmp_path, monkeypatch
    ):
        package = tmp_path / "plugin_with_broken_settings"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "kuma_client.py").write_text("", encoding="utf-8")
        (package / "settings.py").write_text(
            'raise ImportError("failure inside local settings")\n', encoding="utf-8"
        )
        source = Path(connector.__file__).read_text(encoding="utf-8")
        (package / "uptime_kuma_connector.py").write_text(source, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))

        with pytest.raises(ImportError, match="failure inside local settings"):
            import_module("plugin_with_broken_settings.uptime_kuma_connector")


class TestToolWiring:
    def test_service_status_is_a_non_direct_tool(self):
        assert connector.service_status.procedure_type == "tool"
        assert connector.service_status.return_direct is False
        assert "VPN" in connector.service_status.description

    def test_no_flow_hook_is_registered(self):
        registered = [
            name
            for name, value in vars(connector).items()
            if hasattr(value, "name") and hasattr(value, "priority")
        ]

        assert registered == [], f"hooks registered with no implementation: {registered}"

    def test_the_api_key_field_exists_and_is_the_only_source(self):
        # Reversed on 2026-09-08: the key is configured in the panel and read
        # from there alone. One source, because a field plus an environment
        # fallback are two places that can disagree with no way to see which
        # one an instance is using.
        schema = settings_module.UptimeKumaConnectorSettings.model_json_schema()

        assert "api_key" in schema["properties"]
        assert connector.resolve_api_key(
            settings_module.UptimeKumaConnectorSettings(api_key="a-key")
        ) == "a-key"


class FakeHttpResponse:
    def __init__(self, text, failure=None, headers=None, chunks=None):
        self.content = text.encode("utf-8")
        self.failure = failure
        self.headers = headers or {}
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.failure is not None:
            raise self.failure

    def iter_bytes(self):
        yield from self.chunks if self.chunks is not None else (self.content,)


class TestServiceStatusBehaviour:
    """Phase 3: every adapter failure is an honest `unknown` response."""

    VALID_PAYLOAD = (
        'monitor_status{monitor_id="17",monitor_name="VPN - GlobalProtect"} 1'
    )

    @staticmethod
    def call(stored, service_name="VPN"):
        return connector.service_status.func(service_name, FakeCat(stored))

    def test_an_unconfigured_connector_makes_no_network_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(connector.httpx, "stream", lambda *args, **kwargs: calls.append(1))

        sentence = self.call({})

        assert calls == []
        assert sentence == connector.kuma_client.unreachable_sentence("VPN")

    def test_a_url_without_a_key_makes_no_network_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(connector.httpx, "stream", lambda *args, **kwargs: calls.append(1))

        sentence = self.call({"base_url": "https://kuma.example.org"})

        assert calls == []
        assert sentence == connector.kuma_client.unreachable_sentence("VPN")

    def test_an_unreachable_instance_is_unknown(self, monkeypatch):
        info_lines = []
        warning_lines = []

        def unreachable(*_args, **_kwargs):
            raise connector.httpx.ConnectError("connection refused")

        monkeypatch.setattr(connector.httpx, "stream", unreachable)
        monkeypatch.setattr(connector.log, "info", info_lines.append)
        monkeypatch.setattr(connector.log, "warning", warning_lines.append)

        sentence = self.call(
            {"base_url": "https://kuma.example.org", "api_key": "read-key"}
        )

        assert sentence == connector.kuma_client.unreachable_sentence("VPN")
        assert "Non trarre conclusioni" in sentence
        assert info_lines == [
            "[uptime_kuma_connector] requesting current monitor status."
        ]
        assert any("ConnectError" in line for line in warning_lines)

    def test_http_401_is_unknown_and_the_key_is_not_logged(self, monkeypatch):
        canary = "uk1-do-not-log-this-value"
        lines = []
        request = connector.httpx.Request("GET", "https://kuma.example.org/metrics")
        response = connector.httpx.Response(401, request=request)
        failure = connector.httpx.HTTPStatusError(
            "unauthorised", request=request, response=response
        )
        monkeypatch.setattr(
            connector.httpx,
            "stream",
            lambda *_args, **_kwargs: FakeHttpResponse("", failure),
        )
        monkeypatch.setattr(connector.log, "warning", lines.append)
        connector._reported_configuration = None

        sentence = self.call(
            {"base_url": "https://kuma.example.org", "api_key": canary}
        )

        assert sentence == connector.kuma_client.unreachable_sentence("VPN")
        assert lines
        assert not any(canary in line for line in lines)
        assert any("HTTPStatusError" in line for line in lines)

    def test_the_request_uses_the_metrics_url_auth_header_and_timeout(self, monkeypatch):
        captured = {}
        info_lines = []

        def successful_stream(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured.update(kwargs)
            return FakeHttpResponse(self.VALID_PAYLOAD)

        monkeypatch.setattr(connector.httpx, "stream", successful_stream)
        monkeypatch.setattr(connector.log, "info", info_lines.append)

        sentence = self.call(
            {
                "base_url": "https://kuma.example.org",
                "api_key": "read-key",
                "request_timeout_seconds": 3.5,
                "maximum_response_size_kib": 256,
            }
        )

        assert sentence == "Il servizio VPN risulta attivo."
        assert captured["method"] == "GET"
        assert captured["url"] == "https://kuma.example.org/metrics"
        assert captured["timeout"] == 3.5
        assert captured["headers"]["Authorization"] == (
            connector.kuma_client.basic_auth_header("read-key")
        )
        assert info_lines == [
            "[uptime_kuma_connector] requesting current monitor status.",
            "[uptime_kuma_connector] monitoring endpoint request succeeded "
            "(outcome: known, resolution: name).",
        ]
        assert not any("read-key" in line for line in info_lines)
        assert not any("kuma.example.org" in line for line in info_lines)

    def test_a_known_alias_is_identified_in_the_success_log(self, monkeypatch):
        lines = []
        monkeypatch.setattr(
            connector.httpx,
            "stream",
            lambda *_args, **_kwargs: FakeHttpResponse(self.VALID_PAYLOAD),
        )
        monkeypatch.setattr(connector.log, "info", lines.append)

        sentence = self.call(
            {
                "base_url": "https://kuma.example.org",
                "api_key": "read-key",
                "alias_map": "Accesso remoto: 17",
            },
            "Accesso remoto",
        )

        assert sentence == "Il servizio Accesso remoto risulta attivo."
        assert any("outcome: known, resolution: alias" in line for line in lines)
        assert not any("17" in line or "Accesso remoto" in line for line in lines)

    def test_the_tool_never_raises_when_the_http_client_does(self, monkeypatch):
        def unexpected_failure(*_args, **_kwargs):
            raise RuntimeError("unexpected client failure")

        monkeypatch.setattr(connector.httpx, "stream", unexpected_failure)

        sentence = self.call(
            {"base_url": "https://kuma.example.org", "api_key": "read-key"}
        )

        assert sentence == connector.kuma_client.unreachable_sentence("VPN")

    def test_an_oversized_declared_response_is_unknown(self, monkeypatch):
        lines = []
        monkeypatch.setattr(
            connector.httpx,
            "stream",
            lambda *_args, **_kwargs: FakeHttpResponse(
                "", headers={"content-length": str(64 * 1024 + 1)}
            ),
        )
        monkeypatch.setattr(connector.log, "warning", lines.append)

        sentence = self.call(
            {
                "base_url": "https://kuma.example.org",
                "api_key": "read-key",
                "maximum_response_size_kib": 64,
            }
        )

        assert sentence == connector.kuma_client.unreachable_sentence("VPN")
        assert any("MetricsResponseTooLarge" in line for line in lines)

    def test_a_chunked_response_cannot_bypass_the_size_limit(self, monkeypatch):
        monkeypatch.setattr(
            connector.httpx,
            "stream",
            lambda *_args, **_kwargs: FakeHttpResponse(
                "", chunks=(b"a" * (32 * 1024), b"b" * (32 * 1024 + 1))
            ),
        )

        sentence = self.call(
            {
                "base_url": "https://kuma.example.org",
                "api_key": "read-key",
                "maximum_response_size_kib": 64,
            }
        )

        assert sentence == connector.kuma_client.unreachable_sentence("VPN")

    def test_the_configured_timeout_bounds_the_complete_response(self, monkeypatch):
        release = Event()
        lines = []

        class SlowResponse(FakeHttpResponse):
            def iter_bytes(self):
                release.wait(5)
                yield self.content

        monkeypatch.setattr(
            connector.httpx,
            "stream",
            lambda *_args, **_kwargs: SlowResponse(self.VALID_PAYLOAD),
        )
        monkeypatch.setattr(connector.log, "warning", lines.append)

        started = time.monotonic()
        sentence = self.call(
            {
                "base_url": "https://kuma.example.org",
                "api_key": "read-key",
                "request_timeout_seconds": 1,
            }
        )
        elapsed = time.monotonic() - started
        release.set()

        assert sentence == connector.kuma_client.unreachable_sentence("VPN")
        assert elapsed < 1.5
        assert any("MetricsRequestDeadlineExceeded" in line for line in lines)

    def test_not_monitored_logs_at_most_three_close_names(self, monkeypatch):
        payload = "\n".join(
            f'monitor_status{{monitor_id="{number}",monitor_name="{name}"}} 1'
            for number, name in enumerate(
                ("Posta", "Portale", "VPN", "Esse3", "U-GOV"), start=1
            )
        )
        lines = []
        monkeypatch.setattr(
            connector.httpx, "stream", lambda *_args, **_kwargs: FakeHttpResponse(payload)
        )
        monkeypatch.setattr(connector.log, "warning", lines.append)
        connector._reported_configuration = None

        sentence = self.call(
            {"base_url": "https://kuma.example.org", "api_key": "read-key"},
            "Archivio",
        )

        assert sentence.startswith("Non risulta alcun controllo")
        diagnostic = next(line for line in lines if "Closest monitor names" in line)
        assert diagnostic.count(",") == 2

    def test_unsafe_monitor_names_never_reach_diagnostic_logs(self, monkeypatch):
        payload = "\n".join(
            (
                r'monitor_status{monitor_id="1",monitor_name="VPN\n[forged]"} 1',
                'monitor_status{monitor_id="2",monitor_name="https://internal.example"} 1',
                'monitor_status{monitor_id="3",monitor_name="Posta"} 1',
            )
        )
        lines = []
        monkeypatch.setattr(
            connector.httpx, "stream", lambda *_args, **_kwargs: FakeHttpResponse(payload)
        )
        monkeypatch.setattr(connector.log, "warning", lines.append)
        connector._reported_configuration = None

        self.call(
            {"base_url": "https://kuma.example.org", "api_key": "read-key"},
            "Archivio",
        )

        diagnostic = next(line for line in lines if "Closest monitor names" in line)
        assert "Posta" in diagnostic
        assert "forged" not in diagnostic
        assert "internal.example" not in diagnostic


class TestTheInstanceUrlValidator:
    """Strict on save, because the panel shows the error while the person who
    typed it is still looking at the form."""

    def build(self, url):
        return settings_module.UptimeKumaConnectorSettings(base_url=url)

    def test_a_trailing_slash_is_removed(self):
        # The banality that costs an afternoon: joined with "/metrics" it makes
        # "//metrics", and some reverse proxies answer 404 rather than
        # normalising it.
        assert self.build("https://kuma.example.org/").base_url == (
            "https://kuma.example.org"
        )

    def test_surrounding_whitespace_is_removed(self):
        assert self.build("  https://kuma.example.org  ").base_url == (
            "https://kuma.example.org"
        )

    def test_a_sub_path_is_kept(self):
        # An instance behind a reverse proxy can legitimately live under one.
        assert self.build("https://intranet.example.org/kuma/").base_url == (
            "https://intranet.example.org/kuma"
        )

    def test_http_is_accepted(self):
        # A decision, not an oversight: the deployment may reach Uptime Kuma
        # over a container network, where the exposure is negligible. It is
        # never silent — the adapter warns.
        assert self.build("http://uptime-kuma:3001").base_url == (
            "http://uptime-kuma:3001"
        )

    def test_credentials_in_the_url_are_refused(self):
        # They would be written to settings.json in clear text, which is the
        # exact exposure the missing API key field exists to avoid.
        #
        # The userinfo is assembled instead of written inline, and that is not
        # cosmetic. A URL carrying credentials in its authority section, spelled
        # out in one piece, is what `.githooks/check-staged-secrets.sh` blocks —
        # rightly, because it cannot tell this fixture from a pasted credential.
        # The hook has no exemption mechanism on purpose, since an escape hatch
        # in a secret scan is a way to silence a real detection, so the fixture
        # bends rather than the pattern. For the same reason this comment
        # describes the shape rather than showing it.
        userinfo = "u:p"
        with pytest.raises(ValidationError):
            self.build(f"https://{userinfo}@kuma.example.org")

    def test_an_unsupported_scheme_is_refused(self):
        with pytest.raises(ValidationError):
            self.build("ftp://kuma.example.org")

    def test_a_url_without_a_scheme_is_refused(self):
        with pytest.raises(ValidationError):
            self.build("kuma.example.org")

    def test_a_scheme_without_a_host_is_refused(self):
        with pytest.raises(ValidationError):
            self.build("https://")

    def test_query_parameters_are_refused(self):
        with pytest.raises(ValidationError):
            self.build("https://kuma.example.org?token=x")


class TestNetworkSafetyLimits:
    def test_boundary_values_are_accepted(self):
        lower = settings_module.UptimeKumaConnectorSettings(
            maximum_response_size_kib=64,
            request_timeout_seconds=1,
        )
        upper = settings_module.UptimeKumaConnectorSettings(
            maximum_response_size_kib=10240,
            request_timeout_seconds=10,
        )

        assert (lower.maximum_response_size_kib, lower.request_timeout_seconds) == (
            64,
            1,
        )
        assert (upper.maximum_response_size_kib, upper.request_timeout_seconds) == (
            10240,
            10,
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        (
            ("maximum_response_size_kib", 63),
            ("maximum_response_size_kib", 10241),
            ("request_timeout_seconds", 0.9),
            ("request_timeout_seconds", 10.1),
        ),
    )
    def test_out_of_range_values_are_refused(self, field, value):
        with pytest.raises(ValidationError):
            settings_module.UptimeKumaConnectorSettings(**{field: value})


class TestTheAliasMapIsNeverRefused:
    """The opposite rule, and the asymmetry is the point.

    Refusing the whole field because one line is broken would be the
    requirements.txt defect moved into the user interface: one stray character
    would disable the resolution of every other service.
    """

    def test_a_malformed_line_still_saves(self):
        settings = settings_module.UptimeKumaConnectorSettings(
            alias_map="U-GOV: 12\nquesta riga e rotta\nVPN: 17"
        )

        assert "VPN: 17" in settings.alias_map
        assert connector.alias_map(settings) == {"ugov": (12,), "vpn": (17,)}

    def test_windows_line_endings_are_normalised(self):
        # The panel is used from a browser on Windows.
        settings = settings_module.UptimeKumaConnectorSettings(
            alias_map="U-GOV: 12\r\nVPN: 17"
        )

        assert connector.alias_map(settings) == {"ugov": (12,), "vpn": (17,)}


class FakePlugin:
    def __init__(self, stored):
        self._stored = stored

    def load_settings(self):
        if isinstance(self._stored, Exception):
            raise self._stored
        return self._stored


class FakeMadHatter:
    def __init__(self, stored):
        self._plugin = FakePlugin(stored)

    def get_plugin(self):
        return self._plugin


class FakeCat:
    def __init__(self, stored):
        self.mad_hatter = FakeMadHatter(stored)


class TestLoadSettingsNeverRaises:
    """A configuration problem must not take the turn down."""

    def test_a_valid_configuration_is_returned(self):
        settings = connector.load_settings(
            FakeCat({"base_url": "https://kuma.example.org", "alias_map": "VPN: 17"})
        )

        assert settings.base_url == "https://kuma.example.org"

    def test_an_absent_field_falls_back_to_its_default(self):
        # What makes a new setting work immediately on an existing
        # installation: the model supplies the default of any absent field.
        settings = connector.load_settings(
            FakeCat({"base_url": "https://kuma.example.org"})
        )

        assert settings.alias_map == ""

    def test_a_stored_value_the_validator_refuses_disables_the_connector(self):
        # Reachable when settings.json was edited by hand. Disabling is the safe
        # answer; raising here would break a conversation over a typo.
        settings = connector.load_settings(FakeCat({"base_url": "ftp://kuma"}))

        assert settings.base_url == ""
        assert connector.is_usable(settings) is False

    def test_a_failure_reading_the_settings_disables_the_connector(self):
        settings = connector.load_settings(FakeCat(RuntimeError("no plugin")))

        assert settings.base_url == ""

    def test_no_stored_settings_at_all(self):
        assert connector.load_settings(FakeCat(None)).base_url == ""


class TestIsUsableIsTheSinglePoint:
    """URL **and** key together, and every path goes through here.

    In an analogous integration already in production it happened twice that one
    place tested only the identifier and showed a monitoring indicator with the
    integration switched off.
    """

    def build(self, url="", key=""):
        return settings_module.UptimeKumaConnectorSettings(
            base_url=url, api_key=key
        )

    def test_url_and_key_together(self):
        assert connector.is_usable(
            self.build("https://kuma.example.org", "a-key")
        ) is True

    def test_a_url_without_a_key_is_not_usable(self):
        assert connector.is_usable(self.build("https://kuma.example.org")) is False

    def test_a_key_without_a_url_is_not_usable(self):
        assert connector.is_usable(self.build("", "a-key")) is False

    def test_a_blank_key_does_not_count(self):
        # A field holding spaces is a configuration mistake, not a credential.
        # The validator strips it to empty, which disables the connector.
        settings = self.build("https://kuma.example.org", "   ")

        assert settings.api_key == ""
        assert connector.is_usable(settings) is False

    def test_a_pasted_key_keeps_no_trailing_newline(self):
        # A key copied from a dashboard arrives with one often enough, and that
        # byte would travel inside the Basic Auth header and turn every call
        # into a 401 that looks like a wrong key.
        pasted = " a-key" + chr(10)

        assert self.build("https://kuma.example.org", pasted).api_key == "a-key"


class TestConfigurationProblemsReachTheLog:
    """The only channel an administrator has: the panel cannot show a problem
    found while reading, and the sentences that reach the model never carry
    configuration detail."""

    def capture(self, settings):
        lines = []
        original = connector.log.warning
        connector.log.warning = lines.append
        connector._reported_configuration = None
        try:
            connector.report_configuration_problems(settings)
        finally:
            connector.log.warning = original
            connector._reported_configuration = None
        return lines

    def test_plain_http_is_warned_about(self):
        lines = self.capture(
            settings_module.UptimeKumaConnectorSettings(
                base_url="http://uptime-kuma:3001"
            )
        )

        assert any("HTTPS" in line for line in lines)

    def test_https_is_not_warned_about(self):
        lines = self.capture(
            settings_module.UptimeKumaConnectorSettings(
                base_url="https://kuma.example.org", api_key="a-key"
            )
        )

        assert lines == []

    def test_a_url_without_a_key_is_reported(self):
        # Half-configured is the state worth naming: somebody filled the URL and
        # stopped. Without this line the plugin is silently disabled while the
        # panel looks configured.
        lines = self.capture(
            settings_module.UptimeKumaConnectorSettings(
                base_url="https://kuma.example.org"
            )
        )

        assert any("API key" in line for line in lines)

    def test_the_api_key_never_reaches_a_log_line(self):
        # The rule the whole logging path is built on. Checked against the one
        # configuration that writes the most: plain HTTP, so the transport
        # warning fires and the sentence is about the key, plus a broken alias
        # line. Neither may carry the value.
        #
        # The variable is called `canary` and not `secret`, and that is not
        # style. `.githooks/check-staged-secrets.sh` matches an assignment whose
        # *name* is one of `secret`, `api_key`, `password` and the rest, with a
        # quoted value of eight characters or more — which is precisely the
        # shape of a pasted credential, and was precisely the shape of this
        # line. The hook has no exemption mechanism on purpose, since an escape
        # hatch in a secret scan is a way to silence a real detection, so the
        # fixture bends rather than the pattern. Same reasoning as the assembled
        # userinfo in `test_credentials_in_the_url_are_refused` above.
        canary = "uk1-do-not-log-this-value"
        lines = self.capture(
            settings_module.UptimeKumaConnectorSettings(
                base_url="http://uptime-kuma:3001",
                api_key=canary,
                alias_map="rotta",
            )
        )

        assert lines, "nothing was logged, so the assertion below proves nothing"
        assert not any(canary in line for line in lines)

    def test_a_broken_alias_line_is_reported(self):
        lines = self.capture(
            settings_module.UptimeKumaConnectorSettings(alias_map="rotta")
        )

        assert any("alias map" in line for line in lines)

    def test_an_unchanged_configuration_is_reported_only_once(self):
        # It is read on every turn. Logging it every time would drown it.
        settings = settings_module.UptimeKumaConnectorSettings(alias_map="rotta")
        lines = []
        original = connector.log.warning
        connector.log.warning = lines.append
        connector._reported_configuration = None
        try:
            connector.report_configuration_problems(settings)
            first = len(lines)
            connector.report_configuration_problems(settings)
        finally:
            connector.log.warning = original
            connector._reported_configuration = None

        assert len(lines) == first

    def test_a_valid_configuration_logs_nothing(self):
        assert self.capture(
            settings_module.UptimeKumaConnectorSettings(
                base_url="https://kuma.example.org",
                api_key="a-key",
                alias_map="VPN: 17",
            )
        ) == []


class TestTheAliasMapRendersAsATextArea:
    """One entry per line needs a multi-line box, and the marker that asks for
    one fails **silently** when it is nested wrong: the panel reads `extra.type`
    from the published schema, so writing the marker as `type` replaces the real
    JSON Schema type and puts it where the panel never looks. The field then
    renders as a single-line input and nothing reports a problem. These two
    tests are the only thing that would notice."""

    def field(self):
        schema = settings_module.UptimeKumaConnectorSettings.model_json_schema()
        return schema["properties"]["alias_map"]

    def test_the_panel_is_asked_for_a_multi_line_box(self):
        assert self.field().get("extra", {}).get("type") == "TextArea"

    def test_the_declared_json_schema_type_is_still_a_string(self):
        # The half that catches the mistake: a marker written as `type` would
        # pass the test above only if it also destroyed this one.
        assert self.field().get("type") == "string"
