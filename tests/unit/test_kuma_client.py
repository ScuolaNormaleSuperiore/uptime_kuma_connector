"""Tests for the pure Uptime Kuma logic.

These tests need no Cheshire Cat and no network. They cover:

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
package name where a bare `import kuma_client` does not resolve. The path fix
below is therefore guarded so that it runs under `pytest` and never inside the
core process — the full reasoning is in `tests/integration/test_connector.py`.
It does not weaken the import: a genuine breakage still fails the tests instead
of skipping them.
"""

import ast
import sys
from pathlib import Path

# Guarded for the reason spelled out in `tests/integration/test_connector.py`:
# under the name the Cat imports this file with, the path fix would make our
# `settings.py` answer a bare `import settings` from a neighbouring plugin. It
# runs under `pytest` and nowhere else.
if not __name__.startswith("cat.plugins."):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    import kuma_client  # noqa: E402  (import after the path fix, on purpose)


class TestModuleContract:
    """Structural properties the pure client must always preserve."""

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
        # planned maintenance as a fault.
        statuses = {
            kuma_client.STATUS_DOWN,
            kuma_client.STATUS_UP,
            kuma_client.STATUS_PENDING,
            kuma_client.STATUS_MAINTENANCE,
        }
        assert len(statuses) == 4


class TestNormaliseName:
    """The fold every comparison is made on."""

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
    """The admin panel's alias map."""

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

    def test_repeated_ids_on_one_line_collapse_to_a_single_match(self):
        # A copy-paste duplicate ("VPN: 17, 17, 17") must not become a
        # duplicated clause in the sentence, or trip the ambiguous-match
        # ceiling for what is really one monitor.
        aliases, problems = kuma_client.parse_alias_map("VPN: 17, 17, 17")

        assert aliases == {"vpn": (17,)}
        assert problems == ()

    def test_repeated_ids_keep_the_order_first_seen(self):
        aliases, _ = kuma_client.parse_alias_map("Esse3: 14, 15, 14, 16, 15")

        assert aliases == {"esse3": (14, 15, 16)}

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


class TestMetricsUrl:
    """Building the single endpoint read by the plugin."""

    def test_plain_instance_url(self):
        assert (
            kuma_client.metrics_url("https://kuma.example.org")
            == "https://kuma.example.org/metrics"
        )

    def test_instance_behind_a_reverse_proxy_subpath(self):
        assert (
            kuma_client.metrics_url("https://intranet.example.org/kuma")
            == "https://intranet.example.org/kuma/metrics"
        )

    def test_empty_instance_url_stays_empty(self):
        assert kuma_client.metrics_url("") == ""


class TestBasicAuthHeader:
    """Uptime Kuma uses an empty Basic Auth username and the key as password."""

    def test_known_key_has_the_expected_header(self):
        # Hand-computed base64 for the UTF-8 bytes of ":secret".
        assert kuma_client.basic_auth_header("secret") == "Basic OnNlY3JldA=="

    def test_empty_key_is_recognisably_unusable(self):
        assert kuma_client.basic_auth_header("") == ""


class TestParseMonitorMetrics:
    """Only the relevant, real-world Prometheus metric family is read."""

    FIXTURE = Path(__file__).parent / "fixtures" / "metrics_sample.txt"

    def test_captured_metrics_provide_all_nine_monitors(self):
        payload = self.FIXTURE.read_text(encoding="utf-8")

        names, statuses = kuma_client.parse_monitor_metrics(payload)

        assert names == {
            1: "VPN - GlobalProtect",
            16: "U-GOV - Autenticazione Cineca",
            28: "Esse3 - Web",
            46: "Esse3 - DB",
            47: "Portale di Ateneo",
            48: "Posta elettronica",
            49: "DNS interno",
            50: "Archivio documentale",
            9001: "Servizio di test non disponibile",
        }
        assert statuses == {
            **{monitor_id: 1 for monitor_id in names if monitor_id != 9001},
            9001: kuma_client.STATUS_DOWN,
        }

    def test_noise_metric_families_are_ignored(self):
        payload = (
            'monitor_response_time{monitor_id="1",monitor_name="VPN"} 12\n'
            "process_cpu_seconds_total 32.4\n"
            'nodejs_version_info{version="v22"} 1\n'
        )

        assert kuma_client.parse_monitor_metrics(payload) == ({}, {})

    def test_malformed_lines_do_not_discard_a_valid_one(self):
        payload = "\n".join(
            (
                'monitor_status{tag="",monitor_id="4",monitor_name="Valid"} 3',
                'monitor_status{monitor_id="5",monitor_name="Truncated" 1',
                "monitor_status 1",
                'monitor_status{monitor_id="6",broken,monitor_name="Broken"} 0',
                'monitor_status{monitor_id="7"} 1',
            )
        )

        assert kuma_client.parse_monitor_metrics(payload) == (
            {4: "Valid"},
            {4: kuma_client.STATUS_MAINTENANCE},
        )

    def test_no_monitor_status_line_returns_empty_mappings(self):
        assert kuma_client.parse_monitor_metrics("# no monitor status\n") == ({}, {})


class TestFindMonitor:
    """Service resolution keeps explicit administrator choices ahead of guesses."""

    NAMES = {
        12: "U-GOV",
        13: "U-GOV - Autenticazione Cineca",
        14: "Esse3 - Web",
        15: "Esse3 - DB",
        17: "VPN - GlobalProtect",
    }

    def test_exact_name_is_not_shadowed_by_a_longer_name(self):
        resolution = kuma_client.find_monitor(self.NAMES, "u_gov")

        assert resolution.matches == ((12, "U-GOV"),)
        assert not resolution.used_alias

    def test_normalisation_and_containment_find_a_monitor(self):
        resolution = kuma_client.find_monitor(
            {13: "U-GOV - Autenticazione Cineca"}, "autenticazione UGOV"
        )

        assert resolution.matches == ((13, "U-GOV - Autenticazione Cineca"),)

    def test_a_one_character_query_does_not_match_unrelated_monitors(self):
        # A single letter is a substring of almost any name; without a minimum
        # length this would report three unrelated services as matches.
        resolution = kuma_client.find_monitor(
            {1: "Anagrafica", 2: "Archivio", 3: "API Gateway"}, "A"
        )

        assert resolution.matches == ()

    def test_a_short_query_still_matches_a_whole_word_in_the_name(self):
        # The length guard applies to plain substring containment only: a
        # short query that is a whole word of the name still matches through
        # the word-set fallback, so a legitimate short acronym is unaffected.
        resolution = kuma_client.find_monitor({4: "Portale PA"}, "PA")

        assert resolution.matches == ((4, "Portale PA"),)

    def test_an_explicit_alias_wins_over_name_matching(self):
        resolution = kuma_client.find_monitor(
            self.NAMES,
            "VPN",
            {"vpn": (14,)},
        )

        assert resolution.matches == ((14, "Esse3 - Web"),)
        assert resolution.used_alias

    def test_an_alias_can_select_several_monitors(self):
        resolution = kuma_client.find_monitor(
            self.NAMES,
            "Esse3",
            {"esse3": (14, 15)},
        )

        assert resolution.matches == ((14, "Esse3 - Web"), (15, "Esse3 - DB"))
        assert resolution.missing_alias_ids == ()

    def test_an_alias_reports_missing_ids_without_losing_present_matches(self):
        resolution = kuma_client.find_monitor(
            self.NAMES,
            "Esse3",
            {"esse3": (14, 99)},
        )

        assert resolution.matches == ((14, "Esse3 - Web"),)
        assert resolution.missing_alias_ids == (99,)

    def test_an_alias_with_only_missing_ids_is_not_an_ordinary_miss(self):
        resolution = kuma_client.find_monitor(
            self.NAMES,
            "VPN",
            {"vpn": (99,)},
        )

        assert resolution.matches == ()
        assert resolution.used_alias
        assert resolution.missing_alias_ids == (99,)

    def test_the_request_is_truncated_before_alias_resolution(self):
        alias = "a" * 80
        resolution = kuma_client.find_monitor(
            self.NAMES,
            f"{alias} ignored suffix",
            {alias: (17,)},
        )

        assert resolution.requested_name == alias
        assert resolution.matches == ((17, "VPN - GlobalProtect"),)

    def test_control_characters_are_stripped_from_the_requested_name(self):
        # `requested_name` reaches both the sentence for the model and, in the
        # adapter, a log.warning() call. A newline here must not be able to
        # forge what looks like a second, unrelated log line.
        resolution = kuma_client.find_monitor(
            self.NAMES, "VPN\n[uptime-kuma] fake log line"
        )

        assert "\n" not in resolution.requested_name
        assert resolution.requested_name == "VPN [uptime-kuma] fake log line"


class TestMonitoredServiceNames:
    def test_names_keep_the_metric_order_and_exclude_ids(self):
        assert kuma_client.monitored_service_names(
            {17: "VPN - GlobalProtect", 14: "Esse3 - Web"}
        ) == ("VPN - GlobalProtect", "Esse3 - Web")


class TestDescribeResolution:
    """`describe_status()` is a thin wrapper around this function: the adapter
    calls it directly, with a `MonitorResolution` it already computed, so it
    does not have to parse `/metrics` and resolve the request a second time
    just to log administrator diagnostics."""

    def test_matches_describe_status_for_the_same_input(self):
        payload = (
            'monitor_status{monitor_id="17",monitor_name="VPN - GlobalProtect"} 1'
        )
        names, statuses = kuma_client.parse_monitor_metrics(payload)
        resolution = kuma_client.find_monitor(names, "VPN")

        assert kuma_client.describe_resolution(resolution, statuses) == (
            kuma_client.describe_status(payload, "VPN")
        )


class TestDescribeStatus:
    """The sentence is a product response, not an implementation detail."""

    @staticmethod
    def payload(*monitors: tuple[int, str, int]) -> str:
        return "\n".join(
            (
                f'monitor_status{{tag="",monitor_id="{monitor_id}",'
                f'monitor_name="{name}"}} {status}'
                for monitor_id, name, status in monitors
            )
        )

    def test_each_known_status_has_its_own_sentence(self):
        cases = (
            (kuma_client.STATUS_UP, "Il servizio VPN risulta attivo."),
            (
                kuma_client.STATUS_DOWN,
                "Il servizio VPN non risulta attualmente attivo.",
            ),
            (
                kuma_client.STATUS_PENDING,
                "Il servizio VPN presenta rilevazioni intermittenti.",
            ),
            (
                kuma_client.STATUS_MAINTENANCE,
                "Il servizio VPN è in manutenzione programmata.",
            ),
        )

        for status, sentence in cases:
            outcome, result = kuma_client.describe_status(
                self.payload((17, "VPN", status)), "VPN"
            )

            assert outcome == kuma_client.OUTCOME_KNOWN
            assert result == sentence

    def test_unrecognised_status_is_unknown_not_down(self):
        outcome, sentence = kuma_client.describe_status(
            self.payload((17, "VPN", 9)), "VPN"
        )

        assert outcome == kuma_client.OUTCOME_UNKNOWN
        assert sentence == kuma_client.unreachable_sentence("VPN")

    def test_multiple_matches_name_each_service_and_its_state(self):
        outcome, sentence = kuma_client.describe_status(
            self.payload(
                (14, "Esse3 - Web", kuma_client.STATUS_UP),
                (15, "Esse3 - DB", kuma_client.STATUS_DOWN),
            ),
            "Esse3",
        )

        assert outcome == kuma_client.OUTCOME_KNOWN
        assert sentence == (
            "Per Esse3 risultano più controlli: Esse3 - Web risulta attivo; "
            "Esse3 - DB non risulta attualmente attivo."
        )

    def test_more_than_five_matches_is_ambiguous(self):
        payload = self.payload(
            *(
                (monitor_id, f"Portale {monitor_id}", kuma_client.STATUS_UP)
                for monitor_id in range(1, 7)
            )
        )

        outcome, sentence = kuma_client.describe_status(payload, "Portale")

        assert outcome == kuma_client.OUTCOME_AMBIGUOUS
        assert sentence == (
            "Portale corrisponde a troppi controlli per identificare un servizio. "
            "Chiedi all'utente quale servizio intende."
        )

    def test_an_alias_grouping_more_than_five_monitors_is_not_ambiguous(self):
        # An alias is an explicit administrator decision, not a guess: grouping
        # the components of one composite service is intended even above five
        # of them, so it must not collapse into `ambiguous` the way an
        # accidental name/containment match with too many results does.
        payload = self.payload(
            *(
                (monitor_id, f"Componente {monitor_id}", kuma_client.STATUS_UP)
                for monitor_id in range(1, 8)
            )
        )

        outcome, sentence = kuma_client.describe_status(
            payload, "Piattaforma unica", {"piattaforma unica": tuple(range(1, 8))}
        )

        assert outcome == kuma_client.OUTCOME_KNOWN
        assert sentence.startswith("Per Piattaforma unica risultano più controlli:")

    def test_an_alias_grouping_too_many_monitors_is_still_ambiguous(self):
        # The alias ceiling is higher, not absent: past MAXIMUM_ALIAS_MATCHES
        # the sentence would still carry too many names into the prompt.
        payload = self.payload(
            *(
                (monitor_id, f"Componente {monitor_id}", kuma_client.STATUS_UP)
                for monitor_id in range(1, 12)
            )
        )

        outcome, _sentence = kuma_client.describe_status(
            payload, "Piattaforma unica", {"piattaforma unica": tuple(range(1, 12))}
        )

        assert outcome == kuma_client.OUTCOME_AMBIGUOUS

    def test_not_monitored_never_lists_other_services_or_reassures(self):
        outcome, sentence = kuma_client.describe_status(
            self.payload((17, "VPN", kuma_client.STATUS_UP)), "Posta"
        )

        assert outcome == kuma_client.OUTCOME_NOT_MONITORED
        assert sentence == (
            "Non risulta alcun controllo di disponibilità per Posta. Non è "
            "disponibile alcuna informazione sul suo stato: non concluderne che "
            "il servizio funzioni."
        )
        assert "VPN" not in sentence
        assert "attivo" not in sentence

    def test_alias_with_only_missing_ids_is_unknown(self):
        outcome, sentence = kuma_client.describe_status(
            self.payload((17, "VPN", kuma_client.STATUS_UP)),
            "Posta",
            {"posta": (99,)},
        )

        assert outcome == kuma_client.OUTCOME_UNKNOWN
        assert sentence == kuma_client.unreachable_sentence("Posta")

    def test_unparseable_payload_is_unknown(self):
        outcome, sentence = kuma_client.describe_status("not metrics", "VPN")

        assert outcome == kuma_client.OUTCOME_UNKNOWN
        assert sentence == kuma_client.unreachable_sentence("VPN")

    def test_unreachable_sentence_forbids_any_conclusion(self):
        assert kuma_client.unreachable_sentence("VPN") == (
            "Non è stato possibile determinare lo stato di VPN. Non trarre "
            "conclusioni: non affermare né che il servizio è attivo né che è "
            "guasto."
        )

    def test_sentences_do_not_expose_metric_metadata(self):
        payload = (
            'monitor_status{monitor_id="17",monitor_name="VPN",'
            'monitor_url="https://internal.example.org"} 1'
        )

        _, sentence = kuma_client.describe_status(payload, "VPN")

        assert "17" not in sentence
        assert "http" not in sentence.casefold()
        assert "internal.example.org" not in sentence
