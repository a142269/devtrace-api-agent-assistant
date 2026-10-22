"""Base-name normalization for holdout tool checks.

The labels were written with the local tool spelling while the MCP transport prefixes
them, so a supplementary lens is needed. These tests hold the lens to two promises:
it never touches the default assess() behaviour, and it can only re-attribute a
tool-name binding - never a real evidence or answer failure.
"""
import copy
import json
import unittest
from pathlib import Path

from developer.evaluate_holdout import (
    MCP_TRANSPORT_PREFIX,
    load_cases,
    normalize_tool_name,
    rescore_report,
    rescore_row,
    assess,
    tool_reasons,
)
from developer.planner import TOOL_SCHEMAS

EVAL_DIR = Path(__file__).resolve().parent.parent / "developer"


def response(*tools, **extra):
    payload = {"plan": {"status": "complete"}, "actions": [{"tool": tool} for tool in tools],
               "evidence": [], "diagnosis": {}, "answer": "", "security": {},
               "decision_mode": "model"}
    payload.update(extra)
    return payload


class NormalizeToolNameTests(unittest.TestCase):
    def test_every_registered_tool_twin_maps_back_to_its_base_name(self):
        for name in TOOL_SCHEMAS:
            self.assertEqual(normalize_tool_name(MCP_TRANSPORT_PREFIX + name), name)

    def test_known_public_pair(self):
        self.assertEqual(normalize_tool_name("mcp_lookup_error_playbook"), "lookup_error_playbook")
        self.assertEqual(normalize_tool_name("mcp_search_public_docs"), "search_public_docs")

    def test_local_spelling_is_returned_unchanged(self):
        self.assertEqual(normalize_tool_name("lookup_error_playbook"), "lookup_error_playbook")

    def test_unknown_prefixed_name_is_never_stripped(self):
        for name in ("mcp_ext_something", "mcp_", "mcp_not_a_tool", "mcp_delete_everything"):
            self.assertEqual(normalize_tool_name(name), name)

    def test_registry_itself_decides_what_counts_as_known(self):
        self.assertIn("lookup_error_playbook", TOOL_SCHEMAS)
        self.assertNotIn("delete_everything", TOOL_SCHEMAS)

    def test_case_and_whitespace_variants_do_not_fire(self):
        base = "lookup_error_playbook"
        for name in (f"MCP_{base}", f"Mcp_{base}", f" {MCP_TRANSPORT_PREFIX}{base}",
                     f"{MCP_TRANSPORT_PREFIX}{base} ", f"{MCP_TRANSPORT_PREFIX} {base}"):
            self.assertEqual(normalize_tool_name(name), name)

    def test_non_strings_pass_through(self):
        for value in (None, 12, ["mcp_search_public_docs"], ""):
            self.assertEqual(normalize_tool_name(value), value)


class AssessNormalizationTests(unittest.TestCase):
    expect = {"tools_include": ["lookup_error_playbook"]}

    def test_default_assess_still_requires_the_label_spelling(self):
        row = assess("c", "public_api", response("mcp_lookup_error_playbook"), self.expect)
        self.assertFalse(row["passed"])
        self.assertEqual(row["reasons"], ["missing_tool:lookup_error_playbook"])
        self.assertNotIn("tools_base", row)
        self.assertEqual(row["tools"], ["mcp_lookup_error_playbook"])

    def test_normalized_assess_passes_and_keeps_the_transport_spelling_reported(self):
        row = assess("c", "public_api", response("mcp_lookup_error_playbook"),
                     self.expect, normalize_tools=True)
        self.assertTrue(row["passed"])
        self.assertEqual(row["tools"], ["mcp_lookup_error_playbook"])
        self.assertEqual(row["tools_base"], ["lookup_error_playbook"])

    def test_exclusions_are_stricter_under_normalization_not_looser(self):
        expect = {"tools_exclude": ["get_request_trace"]}
        observed = response("mcp_get_request_trace", "search_public_docs")
        self.assertTrue(assess("c", "auth", observed, expect)["passed"])
        normalized = assess("c", "auth", observed, expect, normalize_tools=True)
        self.assertFalse(normalized["passed"])
        self.assertEqual(normalized["reasons"], ["unexpected_tool:get_request_trace"])

    def test_non_tool_checks_are_untouched_by_the_flag(self):
        expect = {"evidence_contains": ["doc:v1 / 速率限制"]}
        row = assess("c", "auth", response("search_api_docs"), expect, normalize_tools=True)
        self.assertEqual(row["reasons"], ["evidence_missing:doc:v1 / 速率限制"])


class RescoreRowTests(unittest.TestCase):
    def test_label_binding_failure_is_reattributed(self):
        row = {"id": "h_public_playbook_path", "group": "public_api", "passed": False,
               "reasons": ["missing_tool:lookup_error_playbook"],
               "tools": ["mcp_lookup_error_playbook"], "batch": "2026-10-21",
               "expected": [{"tools_include": ["lookup_error_playbook"]}]}
        result = rescore_row(row)
        self.assertTrue(result["rescored"])
        self.assertTrue(result["base_names_passed"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["binding_fixed"], ["missing_tool:lookup_error_playbook"])
        self.assertEqual(result["still_failed_reasons"], [])

    def test_non_tool_failure_stays_failed(self):
        row = {"id": "h_quota_v2_doc_version_boundary", "group": "auth", "passed": False,
               "reasons": ["evidence_missing:doc:v1 / 速率限制"],
               "tools": ["get_request_trace", "search_api_docs"],
               "expected": [{"hypotheses": {"minute_quota_exceeded": "supported"},
                             "evidence_contains": ["doc:v1 / 速率限制"]}]}
        result = rescore_row(row)
        self.assertTrue(result["rescored"])
        self.assertFalse(result["base_names_passed"])
        self.assertEqual(result["binding_fixed"], [])
        self.assertEqual(result["still_failed_reasons"], ["evidence_missing:doc:v1 / 速率限制"])

    def test_multi_step_rows_are_refused_not_guessed(self):
        row = {"id": "h_multiturn_missing_then_id#2", "group": "multiturn", "passed": False,
               "reasons": ["step1:missing_tool:get_request_trace"],
               "tools": ["mcp_get_request_trace"], "expected": [{}, {}]}
        result = rescore_row(row)
        self.assertFalse(result["rescored"])
        self.assertIn("multi-step", result["why_not_rescored"])
        self.assertFalse(result["base_names_passed"])

    def test_unreproducible_stored_reasons_are_refused(self):
        row = {"id": "x", "group": "auth", "passed": False,
               "reasons": ["missing_tool:get_webhook_delivery"],
               "tools": ["get_request_trace"], "expected": [{"plan_status": "complete"}]}
        result = rescore_row(row)
        self.assertFalse(result["rescored"])
        self.assertIn("do not reproduce", result["why_not_rescored"])

    def test_tool_reasons_helper_matches_assess_for_both_spellings(self):
        expect = {"tools_include": ["search_public_docs"], "tools_first": "search_public_docs"}
        self.assertEqual(tool_reasons(["mcp_search_public_docs"], expect),
                         ["missing_tool:search_public_docs", "first_tool=['mcp_search_public_docs']"])
        self.assertEqual(tool_reasons(["search_public_docs"], expect), [])


class RescoreReportTests(unittest.TestCase):
    def test_report_splits_official_and_base_names_without_touching_the_input(self):
        official = {"summary": {"count": 2, "passed": 1},
                    "labels": "hand-written",
                    "cases": [
                        {"id": "a", "group": "public_api", "passed": False, "batch": "2026-10-21",
                         "decision_mode": "model",
                         "reasons": ["missing_tool:lookup_error_playbook"],
                         "tools": ["mcp_lookup_error_playbook"],
                         "expected": [{"tools_include": ["lookup_error_playbook"]}]},
                        {"id": "b", "group": "quota", "passed": True, "batch": "2026-10-18",
                         "decision_mode": "model", "reasons": [], "tools": ["search_quota_docs"],
                         "expected": [{"tools_include": ["search_quota_docs"]}]},
                    ]}
        snapshot = copy.deepcopy(official)
        report = rescore_report(official)
        self.assertEqual(official, snapshot)
        self.assertEqual(report["official"]["summary"], {"count": 2, "passed": 1})
        self.assertEqual(report["base_names"]["summary"]["count"], 2)
        self.assertEqual(report["base_names"]["summary"]["passed"], 2)
        self.assertEqual([row["id"] for row in report["base_names"]["changed_cases"]], ["a"])
        self.assertEqual(report["base_names"]["still_failing"], [])
        self.assertEqual(report["cost"]["model_calls"], 0)

    def test_shipped_p8_report_rescores_to_39_of_40_offline(self):
        path = EVAL_DIR / "eval_holdout_model_2026-10-22_full40.json"
        official = json.loads(path.read_text(encoding="utf-8"))
        report = rescore_report(official)
        self.assertEqual(official["summary"]["passed"], 37)
        self.assertEqual(report["base_names"]["summary"]["passed"], 39)
        changed = sorted(row["id"] for row in report["base_names"]["changed_cases"])
        self.assertEqual(changed, ["h_public_docs_path", "h_public_playbook_path"])
        self.assertEqual(
            sorted(row["id"] for row in report["base_names"]["still_failing"]),
            ["h_quota_v2_doc_version_boundary"])
        self.assertEqual(len(report["base_names"]["not_rescored"]), 3)

    def test_normalization_fixes_only_label_bindings_in_the_real_set(self):
        official = json.loads(
            (EVAL_DIR / "eval_holdout_model_2026-10-22_full40.json").read_text(encoding="utf-8"))
        for row in rescore_report(official)["cases"]:
            if row["changed"]:
                self.assertTrue(row["binding_fixed"], row["id"])
                self.assertTrue(all(reason.startswith(("missing_tool:", "unexpected_tool:",
                                                       "first_tool="))
                                    for reason in row["official_reasons"]), row["id"])

    def test_every_case_id_in_the_stored_report_still_exists_in_the_labels(self):
        official = json.loads(
            (EVAL_DIR / "eval_holdout_model_2026-10-22_full40.json").read_text(encoding="utf-8"))
        label_ids = set()
        for case in load_cases():
            label_ids.add(case["id"])
            for index in range(len(case.get("steps") or [])):
                label_ids.add(f"{case['id']}#{index + 1}")
        for row in official["cases"]:
            self.assertIn(row["id"], label_ids)


if __name__ == "__main__":
    unittest.main()
