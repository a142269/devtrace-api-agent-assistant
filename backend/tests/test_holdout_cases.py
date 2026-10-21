"""Structural guards for the hand-written holdout set (rules run happens in CI)."""
import unittest
from pathlib import Path

import sys

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from developer import data  # noqa: E402
from developer.assistant import IDENTIFIER_RE  # noqa: E402
from developer.evaluate_holdout import (  # noqa: E402
    EXPECT_KEYS, HYPOTHESIS_CODES, ORIGINAL_BATCH, load_cases,
)

# Identifiers the set references on purpose so the agent must report "no record".
MISSING_RECORD_IDS = {"req_auth_99", "req_auth_98", "delivery_99"}
REQUIRED_GROUPS = {
    "auth", "quota", "webhook", "combo", "injection", "anomaly",
    "ambiguity", "negative", "multiturn", "conflict", "public_api",
}


def identifiers(case):
    steps = case.get("steps") or [{"message": case["message"]}]
    found = []
    for step in steps:
        match = IDENTIFIER_RE.search(step["message"])
        if match:
            found.append(match.group(1).lower())
    return found


class HoldoutSetTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases()

    def test_set_size_and_unique_ids(self):
        self.assertEqual(len(self.cases), 40)
        self.assertEqual(len({case["id"] for case in self.cases}), 40)

    def test_batches_stay_traceable(self):
        counts = {}
        for case in self.cases:
            counts[case.get("batch", ORIGINAL_BATCH)] = counts.get(
                case.get("batch", ORIGINAL_BATCH), 0) + 1
        self.assertEqual(counts, {ORIGINAL_BATCH: 19, "2026-10-21": 21})

    def test_every_group_is_covered(self):
        self.assertTrue(REQUIRED_GROUPS.issubset({case["group"] for case in self.cases}))

    def test_expanded_groups_have_minimum_cases(self):
        groups = {}
        for case in self.cases:
            groups[case["group"]] = groups.get(case["group"], 0) + 1
        for group, minimum in {"public_api": 2, "injection": 4, "combo": 4,
                               "multiturn": 3, "quota": 3}.items():
            with self.subTest(group=group):
                self.assertGreaterEqual(groups.get(group, 0), minimum)

    def test_referenced_records_exist_in_the_mock_platform(self):
        for case in self.cases:
            for identifier in identifiers(case):
                with self.subTest(case=case["id"], identifier=identifier):
                    if identifier in MISSING_RECORD_IDS:
                        continue
                    known = (identifier in data.REQUESTS or identifier in data.WEBHOOK_CASES
                             or identifier in data.KEYS)
                    self.assertTrue(known, f"{identifier} has no fixture")

    def test_key_prefixes_used_by_the_set_exist(self):
        for case in self.cases:
            for expect in [step["expect"] for step in
                           (case.get("steps") or [{"expect": case.get("expect", {})}])]:
                for fragment in expect.get("evidence_contains") or []:
                    with self.subTest(case=case["id"], evidence=fragment):
                        if not fragment.startswith("inspect_key_scope:"):
                            continue
                        prefix = fragment.split(":", 1)[1]
                        self.assertIn(prefix, data.KEYS)

    def test_no_case_claims_an_unknown_expectation_key(self):
        for case in self.cases:
            for step in case.get("steps") or [{"expect": case["expect"]}]:
                self.assertFalse(set(step["expect"]) - EXPECT_KEYS)

    def test_only_known_hypothesis_codes_are_asserted(self):
        for case in self.cases:
            for step in case.get("steps") or [{"expect": case["expect"]}]:
                for code in (step["expect"].get("hypotheses") or {}):
                    self.assertIn(code, HYPOTHESIS_CODES)

    def test_new_cases_label_tools_and_evidence(self):
        for case in self.cases:
            if case.get("batch") != "2026-10-21":
                continue
            for step in case.get("steps") or [{"expect": case["expect"]}]:
                expect = step["expect"]
                with self.subTest(case=case["id"]):
                    labelled = any(key in expect for key in
                                   ("tools_include", "tools_first", "tools_exclude",
                                    "hypotheses", "hypotheses_empty"))
                    self.assertTrue(labelled, "case asserts neither tools nor hypotheses")

    def test_multiturn_cases_resume_to_a_decision(self):
        for case in self.cases:
            if not case.get("steps"):
                continue
            with self.subTest(case=case["id"]):
                self.assertEqual(case["steps"][0]["expect"].get("plan_status"), "waiting")
                self.assertTrue(case["steps"][-1]["expect"].get("hypotheses")
                                or case["steps"][-1]["expect"].get("plan_status") == "complete")


if __name__ == "__main__":
    unittest.main()
