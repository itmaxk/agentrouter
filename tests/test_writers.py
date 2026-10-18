import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrouter import config
from agentrouter.writers import jsonc
from agentrouter.writers.hermes import HermesWriter
from agentrouter.writers.opencode import OpencodeWriter

OPENCODE = """{
  "$schema": "https://opencode.ai/config.json",
  "model": "agentrouter/claude-opus-5-5",
  "provider": {
    "agentrouter": {
      "options": {
        // proxy
        "baseURL": "http://127.0.0.1:18777/v1"
      },
      "models": {
        // not in the catalog
        "claude-opus-5-5": {
          "name": "Claude Opus 5.5",
          "tool_call": true
        }
      }
    }
  }
}
"""

HERMES = """_config_version: 50
model:
  default: gpt-6.1-sol-900k
  provider: openai-codex
command_allowlist:
  - script execution via -e/-c flag
fallback_providers:
  - provider: anthropic
    model: claude-opus-5-5

streaming:
  enabled: false
"""


class OpencodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "opencode.jsonc").write_text(OPENCODE)
        (d / "models.json").write_text('{"agentrouter": {"models": {"deepseek-v4-flash": {}}}}')
        self.w = OpencodeWriter({"path": str(d / "opencode.jsonc"), "provider": "agentrouter",
                                 "catalog": str(d / "models.json")})

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalog_model_only_changes_model_line(self):
        out = self.w.render(OPENCODE, "deepseek-v4-flash")
        self.assertEqual(out, OPENCODE.replace('"agentrouter/claude-opus-5-5"', '"agentrouter/deepseek-v4-flash"'))

    def test_uncatalogued_model_gets_declared_and_comments_survive(self):
        out = self.w.render(OPENCODE, "gpt-x")
        data = jsonc.loads(out)
        self.assertEqual(data["model"], "agentrouter/gpt-x")
        self.assertIn("gpt-x", data["provider"]["agentrouter"]["models"])
        self.assertIn("// proxy", out)
        self.assertIn("// not in the catalog", out)
        self.assertEqual(self.w.render(out, "gpt-x"), out)

    def test_current_and_apply(self):
        self.assertEqual(self.w.current(), "claude-opus-5-5")
        with mock.patch("agentrouter.writers.opencode.backup"):
            self.assertTrue(self.w.apply("deepseek-v4-flash"))
            self.assertFalse(self.w.apply("deepseek-v4-flash"))
        self.assertEqual(self.w.current(), "deepseek-v4-flash")


class HermesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = Path(self.tmp.name) / "config.yaml"
        p.write_text(HERMES)
        cfg = config.load(Path(self.tmp.name) / "none.json")
        cfg["user_agent"] = "opencode/test"
        self.w = HermesWriter({"path": str(p), "provider": "agentrouter", "keep_existing": True}, cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rewrites_only_fallback_block_and_adds_provider(self):
        out = self.w.render(HERMES, "claude-opus-5-5")
        head, tail = HERMES.split("fallback_providers:")
        self.assertTrue(out.startswith(head))
        self.assertIn("\n\nstreaming:\n  enabled: false\n", out)
        import yaml
        data = yaml.safe_load(out)
        self.assertEqual(data["fallback_providers"], [
            {"provider": "agentrouter", "model": "claude-opus-5-5", "api_mode": "chat_completions"},
            {"provider": "anthropic", "model": "claude-opus-5-5"}])
        self.assertEqual(data["providers"]["agentrouter"]["extra_headers"], {"User-Agent": "opencode/test"})
        self.assertEqual(data["streaming"], {"enabled": False})

    def test_switch_keeps_provider_block_and_is_idempotent(self):
        once = self.w.render(HERMES, "claude-opus-5-5")
        twice = self.w.render(once, "gpt-6.1-sol", "responses")
        self.assertEqual(twice.count("providers:\n  agentrouter:"), 1)
        self.assertIn("api_mode: codex_responses", twice)
        self.assertEqual(self.w.render(twice, "gpt-6.1-sol", "responses"), twice)
        Path(self.w.path).write_text(twice)
        self.assertEqual(self.w.current(), "gpt-6.1-sol")


if __name__ == "__main__":
    unittest.main()
