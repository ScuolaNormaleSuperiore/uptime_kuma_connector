"""Tests for the Cheshire Cat adapter.

**No behaviour is implemented, so there is no behaviour to test.** What is left
is the structure and the checks that hold anyway: that the plugin loads, that it
registers what it claims to register, and — the reason this file exists now
rather than later — that it registers nothing it does not yet implement.

These need the core importable, because the module under test imports `cat.log`
and `cat.mad_hatter.decorators` at import time. They never contact a live
instance: the container is used as an interpreter, not as a server.

    docker compose exec -w /app/cat/plugins/uptime_kuma_connector \\
        cheshire-cat-core python -m pytest

The tests to write once the implementation starts are listed in
`DOC/Specifiche.md`, section 6. The ones that belong here:

- connector not configured: no network call is attempted
- URL configured but API key missing: no call, outcome `unknown`
- **instance unreachable: outcome `unknown`, and the sentence claims neither
  that the service is up nor that it is down.** This is the one that matters
  most, because it is the only failure that actively misleads a user
- HTTP 401: outcome `unknown`, and the key appears in no log line
- the timeout is honoured
- the tool never raises, whatever the call does
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import settings as settings_module  # noqa: E402
import uptime_kuma_connector as connector  # noqa: E402


class TestPluginLoads:
    def test_the_settings_model_is_exposed_to_the_admin_panel(self):
        # The core builds the form from this. It answers with an empty model on
        # purpose: the plugin reads no configuration because it does nothing.
        assert settings_module.settings_model.function() is (
            settings_module.UptimeKumaConnectorSettings
        )

    def test_the_settings_form_is_empty_on_purpose(self):
        # An empty form is the honest rendering of a plugin with no behaviour.
        # When the first field arrives this test is the one that has to change,
        # which is the point: adding a field becomes a deliberate act.
        schema = settings_module.UptimeKumaConnectorSettings.model_json_schema()

        assert schema.get("properties", {}) == {}

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


class TestNothingIsWiredYet:
    """The invariant that keeps an empty plugin honest.

    A skeleton that registers a tool would claim a capability it does not have:
    the model would see `service_status` among its options, call it, and get
    nothing back. These tests fail the moment something is wired without being
    implemented — which is exactly when someone would otherwise not notice.
    """

    def test_no_tool_is_registered(self):
        registered = [
            name
            for name, value in vars(connector).items()
            if hasattr(value, "procedure_type")
        ]

        assert registered == [], f"tools registered with no implementation: {registered}"

    def test_no_flow_hook_is_registered(self):
        registered = [
            name
            for name, value in vars(connector).items()
            if hasattr(value, "name") and hasattr(value, "priority")
        ]

        assert registered == [], f"hooks registered with no implementation: {registered}"

    def test_the_adapter_makes_no_network_call(self):
        # No httpx import yet, and therefore no request, no timeout and no
        # failure path. `requirements.txt` declares httpx ahead of use on
        # purpose: a dependency missing at activation time is the expensive
        # failure, so it is declared before the first line that needs it.
        tree = ast.parse(Path(connector.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert not imported & {"httpx", "requests", "urllib", "http", "aiohttp"}

    def test_there_is_no_settings_field_for_the_api_key(self):
        # It must come from the environment. The core persists settings to
        # settings.json in clear text, and this key grants read access to the
        # whole monitoring system, so a panel field is not an acceptable
        # fallback. This test is what stops one being added by convenience.
        schema = settings_module.UptimeKumaConnectorSettings.model_json_schema()
        fields = " ".join(schema.get("properties", {})).lower()

        assert "api_key" not in fields
        assert "token" not in fields
