"""Unit tests for the dependency audit gate."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from developer.audit_gate import evaluate, main


def report_with(*vulns):
    return {"dependencies": [{"name": "chromadb", "version": "0.5.23", "vulns": list(vulns)}]}


class AuditGateTests(unittest.TestCase):
    def test_unfixed_findings_do_not_block(self):
        actionable, known = evaluate(report_with({"id": "PYSEC-2026-3813", "fix_versions": []}))
        self.assertEqual(actionable, [])
        self.assertEqual([item["id"] for item in known], ["PYSEC-2026-3813"])

    def test_fixable_findings_are_actionable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit.json"
            path.write_text(json.dumps(report_with(
                {"id": "PYSEC-2026-1", "fix_versions": ["9.9.9"]},
                {"id": "PYSEC-2026-2", "fix_versions": []})), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([str(path)])
        self.assertEqual(code, 1)
        self.assertIn("PYSEC-2026-1", output.getvalue())
        self.assertIn("upgrade required", output.getvalue())

    def test_missing_report_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([str(Path(temp) / "missing.json")])
        self.assertEqual(code, 0)
        self.assertIn("gate skipped", output.getvalue())

    def test_all_clear_returns_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit.json"
            path.write_text(json.dumps({"dependencies": []}), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([str(path)]), 0)


if __name__ == "__main__":
    unittest.main()
