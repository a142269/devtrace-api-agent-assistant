"""Prompt template versioning and rendering tests."""
import json
import unittest
from pathlib import Path

from developer import evaluate_prompts
from developer.prompts import PROMPT_VERSION, PROMPTS, render


class PromptTests(unittest.TestCase):
    def test_snapshot_matches_rendered_prompts(self):
        snapshot = json.loads(Path(evaluate_prompts.SNAPSHOT_PATH).read_text(encoding="utf-8"))
        self.assertIn(PROMPT_VERSION, {snapshot.get('version'), PROMPT_VERSION})
        self.assertEqual(set(snapshot['hashes']), set(PROMPTS))
        self.assertEqual(evaluate_prompts.fingerprints(), snapshot['hashes'])

    def test_prompts_keep_safety_contracts(self):
        for name, needles in evaluate_prompts.CONTRACTS.items():
            rendered = render(name, **evaluate_prompts.FIXTURES[name])
            for needle in needles:
                with self.subTest(prompt=name, needle=needle):
                    self.assertIn(needle, rendered)

    def test_unknown_prompt_is_rejected(self):
        with self.assertRaises(KeyError):
            render('missing-prompt')

    def test_prompt_metadata_does_not_expose_text(self):
        from developer.prompts import describe

        metadata = describe()
        self.assertEqual(metadata['version'], PROMPT_VERSION)
        self.assertEqual(set(metadata['prompts']), set(PROMPTS))
        self.assertNotIn('你是开发者平台接入诊断 Agent', json.dumps(metadata, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
