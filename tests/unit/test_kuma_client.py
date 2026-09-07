"""Tests for the pure Uptime Kuma logic.

**The real tests have been removed together with the implementation.** What is
left is the structure and one smoke test, so the suite stays runnable and green
and the `pre-commit` hook keeps working — pytest exits non-zero when it collects
nothing at all, which would block every commit.

The tests to write are listed in `DOC/Specifiche.md`, section 6. The ones that
belong here, on the pure logic, with no Cheshire Cat and no network:

- parsing of `/metrics` against a **real response**, not a hand-written mock:
  the label format is the part most easily assumed wrong
- each of the four statuses, with maintenance told apart from a fault
- an exact name match not shadowed by a longer one that contains it
- an ambiguous name matching nothing
- a malformed `/metrics` line or a missing label: no exception
- a response carrying no `monitor_status` line at all

These import only `kuma_client`, which imports nothing from `cat`, so `pytest`
alone is enough: no running Cheshire Cat, no container.

    python -m pytest

The Cat imports every `.py` in the plugin folder, this file included, under a
package name where a bare `import kuma_client` does not resolve — which would
make the core log a plugin load error on every activation. Putting the plugin
folder on the path fixes that without weakening the import: a genuine breakage
still fails the tests instead of skipping them.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import kuma_client  # noqa: E402  (import after the path fix, on purpose)


class TestModuleContract:
    """The two properties that must hold even with nothing implemented."""

    def test_the_pure_module_imports_nothing_from_cat(self):
        # The reason this module exists separately, and the property that keeps
        # it testable without a running Cheshire Cat.
        #
        # Read from the source with `ast` rather than from `sys.modules`: by the
        # time this runs, another test module may legitimately have imported
        # `cat`, so the loaded state cannot tell us who imported it. The source
        # can.
        tree = ast.parse(Path(kuma_client.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert "cat" not in imported, (
            f"kuma_client.py imports from cat: {sorted(imported)}. It must stay "
            "free of the framework so it can be tested with pytest alone."
        )

    def test_the_three_outcomes_are_distinct(self):
        # `unknown` exists precisely so that it is not `down`. If two of these
        # ever collapse into one value, the invariant of the whole plugin —
        # never invent a state — is gone, and no other test would notice.
        outcomes = {
            kuma_client.OUTCOME_KNOWN,
            kuma_client.OUTCOME_NOT_MONITORED,
            kuma_client.OUTCOME_UNKNOWN,
        }
        assert len(outcomes) == 3

    def test_the_four_uptime_kuma_statuses_are_distinct(self):
        # Kept distinct on purpose: collapsing them into up/down would announce
        # planned maintenance as a fault. See Specifiche.md, section 2.4.
        statuses = {
            kuma_client.STATUS_DOWN,
            kuma_client.STATUS_UP,
            kuma_client.STATUS_PENDING,
            kuma_client.STATUS_MAINTENANCE,
        }
        assert len(statuses) == 4
