"""Regression tests for deployment hardening that protects the demo stack."""
import json
import unittest
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parents[1]


def compose():
    return yaml.safe_load((BACKEND / "docker-compose.yml").read_text(encoding="utf-8"))


class DeploymentConfigTests(unittest.TestCase):
    def test_internal_services_publish_no_host_ports(self):
        services = compose()["services"]
        for name in ("chromadb", "redis", "developer-sandbox"):
            with self.subTest(service=name):
                self.assertNotIn("ports", services[name])

    def test_only_the_application_is_exposed(self):
        services = compose()["services"]
        self.assertEqual(services["devtrace"]["ports"], ["8000:8000"])
        self.assertNotIn("ports", services["worker"])
        self.assertEqual(services["worker"]["command"], ["python", "-m", "api.worker"])

    def test_sandbox_healthcheck_targets_its_own_port(self):
        healthcheck = compose()["services"]["developer-sandbox"].get("healthcheck")
        self.assertIn("8100", json.dumps(healthcheck, ensure_ascii=False))

    def test_secret_files_stay_out_of_git_and_the_image(self):
        for name in (".gitignore", ".dockerignore"):
            with self.subTest(file=name):
                self.assertIn(".env", (BACKEND / name).read_text(encoding="utf-8"))

    def test_image_runs_unprivileged_and_declares_healthcheck(self):
        dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER echomind", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("PRELOAD_ONNX", dockerfile)


if __name__ == "__main__":
    unittest.main()
