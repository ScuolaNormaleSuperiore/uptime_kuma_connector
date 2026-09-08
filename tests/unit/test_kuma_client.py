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

    def test_the_four_outcomes_are_distinct(self):
        # `unknown` exists precisely so that it is not `down`, and
        # `not_monitored` exists so that it is not `unknown`: "no check exists"
        # is certain, "I do not know" is our own malfunction. If any two of
        # these collapse into one value, the invariant of the whole plugin —
        # never invent a state — is gone, and no other test would notice.
        outcomes = {
            kuma_client.OUTCOME_KNOWN,
            kuma_client.OUTCOME_NOT_MONITORED,
            kuma_client.OUTCOME_AMBIGUOUS,
            kuma_client.OUTCOME_UNKNOWN,
        }
        assert len(outcomes) == 4

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


class TestNormaliseName:
    """The fold every comparison is made on. See Specifiche.md, section 3.2."""

    def test_case_and_surrounding_whitespace_do_not_matter(self):
        assert kuma_client.normalise_name("  U-GOV  ") == kuma_client.normalise_name(
            "u-gov"
        )

    def test_the_hyphen_is_insignificant(self):
        # The rule that earns its keep: it makes "UGOV" match a monitor called
        # "U-GOV - Autenticazione" without anybody writing an alias for it.
        assert kuma_client.normalise_name("UGOV") in kuma_client.normalise_name(
            "U-GOV - Autenticazione"
        )

    def test_removing_punctuation_does_not_leave_double_spaces(self):
        # Order matters in the implementation: stripping "-" from "A - B" leaves
        # two spaces, which the whitespace pass then has to collapse. Without
        # that order this equals "a  b" and no containment test ever matches.
        assert kuma_client.normalise_name("A - B") == "a b"

    def test_dots_and_underscores_fold_too(self):
        assert kuma_client.normalise_name("srv_web.01") == kuma_client.normalise_name(
            "srvweb01"
        )

    def test_it_survives_junk_input(self):
        # Called with whatever the model passed as an argument.
        assert kuma_client.normalise_name("") == ""
        assert kuma_client.normalise_name(None) == ""


class TestParseAliasMap:
    """The admin panel's alias map. See Specifiche.md, section 3.2."""

    def test_a_single_alias_maps_to_a_single_id(self):
        aliases, problems = kuma_client.parse_alias_map("VPN: 17")

        assert aliases == {"vpn": (17,)}
        assert problems == ()

    def test_several_aliases_share_one_id(self):
        aliases, _ = kuma_client.parse_alias_map("U-GOV, Ugov, Cineca: 12")

        # "U-GOV" and "Ugov" fold to the same key, which is the point: writing
        # both spellings is the natural thing to do and must not be an error.
        assert aliases == {"ugov": (12,), "cineca": (12,)}

    def test_one_alias_can_group_several_monitors(self):
        # How an administrator groups the components of a service explicitly,
        # without depending on how they were named in Uptime Kuma.
        aliases, problems = kuma_client.parse_alias_map("Esse3: 14, 15, 16")

        assert aliases == {"esse3": (14, 15, 16)}
        assert problems == ()

    def test_blank_lines_and_comments_are_ignored(self):
        aliases, problems = kuma_client.parse_alias_map(
            "\n# la mappa dei servizi\n\nVPN: 17\n\n"
        )

        assert aliases == {"vpn": (17,)}
        assert problems == ()

    def test_a_malformed_line_does_not_abandon_the_others(self):
        # **The test this function exists for.** It is the shape of the
        # requirements.txt defect: the core parses that file in a loop inside a
        # try that gives up on the whole list at the first bad line, installing
        # nothing. One stray character must not disable every service.
        aliases, problems = kuma_client.parse_alias_map(
            "U-GOV: 12\nquesta riga e rotta\nEsse3: 14\nPosta: non-un-id\nVPN: 17"
        )

        assert aliases == {"ugov": (12,), "esse3": (14,), "vpn": (17,)}
        assert len(problems) == 2

    def test_every_problem_names_its_line_number(self):
        # The log line is the only channel an administrator has, so it has to
        # say where to look.
        _, problems = kuma_client.parse_alias_map("VPN: 17\nrotta")

        assert problems[0].startswith("line 2:")

    def test_a_line_with_one_bad_id_is_discarded_whole(self):
        # Not half-applied: an alias that answered about some components of a
        # service and stayed silent about the others would be worse than one
        # that does not apply at all.
        aliases, problems = kuma_client.parse_alias_map("Esse3: 14, rotto, 16")

        assert aliases == {}
        assert len(problems) == 1

    def test_a_conflicting_duplicate_keeps_the_first_and_is_reported(self):
        aliases, problems = kuma_client.parse_alias_map("VPN: 17\nVPN: 99")

        assert aliases == {"vpn": (17,)}
        assert len(problems) == 1

    def test_a_harmless_duplicate_is_not_reported(self):
        # Same alias, same ids: nothing is wrong, and a warning here would
        # teach people to ignore the warnings that matter.
        _, problems = kuma_client.parse_alias_map("VPN: 17\nVPN: 17")

        assert problems == ()

    def test_an_empty_map_is_not_a_problem(self):
        # The shipped default. It must not produce a single log line.
        assert kuma_client.parse_alias_map("") == ({}, ())
        assert kuma_client.parse_alias_map(None) == ({}, ())
