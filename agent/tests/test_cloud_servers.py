"""Model server choice: cloud APIs for the main agent, addresses in any form, keys as each server wants them."""
import argparse
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import install
from maxedcode import appsettings, servers


class CloudListTests(unittest.TestCase):
    def test_every_cloud_is_known_and_gets_no_local_cache_fields(self):
        self.assertGreaterEqual(len(servers.CLOUD), 10)
        for label, (kind, url) in servers.CLOUD.items():
            self.assertEqual(servers.cloud_kind(url), kind, label)
            self.assertEqual(servers.kind_of(url), kind, label)       # no probes of local endpoints
            self.assertEqual(servers.LABELS[kind], label)
            self.assertEqual(servers.find_base(url), url, label)      # used as it is, no request
            if kind != "openai":
                self.assertEqual(servers.cache_fields(kind), set(), label)
        self.assertEqual(servers.cloud_kind("http://192.168.0.10:1234/v1"), "")

    def test_anthropic_model_list_gets_its_own_headers(self):
        self.assertEqual(servers.auth_headers("https://api.anthropic.com/v1/models", "k"),
                         {"x-api-key": "k", "anthropic-version": "2023-06-01"})
        self.assertEqual(servers.auth_headers("https://api.groq.com/openai/v1/models", "k"), {"Authorization": "Bearer k"})
        self.assertEqual(servers.auth_headers("http://127.0.0.1:8080/v1/models", "local"), {})

    def test_describe_calls_a_cloud_online_when_its_model_list_answers(self):
        with patch.object(servers, "get", return_value=None), \
                patch.object(servers, "model_ids", return_value=["m1"]) as ids, \
                patch.object(servers, "port_down", side_effect=AssertionError("no local probe")):
            info = servers.describe("https://api.deepseek.com/v1", "k")
        self.assertEqual((info["kind"], info["online"]), ("deepseek", True))
        ids.assert_called_once()


class AddressTests(unittest.TestCase):
    def test_any_form_of_a_network_address(self):
        urls = servers.candidates("192.168.0.10")
        self.assertEqual(urls[:2], ["http://192.168.0.10:8080/v1", "http://192.168.0.10:8080"])
        self.assertIn("http://192.168.0.10:1234/v1", urls)
        self.assertEqual(servers.candidates("pc:1234/v1/chat/completions"), ["http://pc:1234/v1"])
        self.assertEqual(install.candidates("10.0.0.2:11434"), servers.candidates("10.0.0.2:11434", None, install.PORTS))

    def test_find_base_takes_the_first_url_that_answers(self):
        def http(url, key="", body=None, timeout=3):
            if url == "http://192.168.0.10:1234/v1/models":
                raise urllib.error.HTTPError(url, 401, "key", {}, None)      # there, but wants a key
            raise OSError("closed")
        with patch.object(servers, "http", side_effect=http):
            self.assertEqual(servers.find_base("192.168.0.10", "k"), "http://192.168.0.10:1234/v1")
            self.assertIsNone(servers.find_base("192.168.0.11"))
        self.assertEqual(servers.find_base("https://api.x.ai/v1"), "https://api.x.ai/v1")   # one candidate: no request


class InstallerCloudTests(unittest.TestCase):
    def setUp(self):
        install.ARGS = argparse.Namespace(yes=False)

    def test_cloud_row_picks_a_provider(self):
        names = list(install.CLOUD)
        gemini = names.index("Google Gemini")
        with patch.object(install, "select", return_value=gemini) as menu, redirect_stdout(io.StringIO()):
            self.assertEqual(install.pick_cloud("https://api.anthropic.com/v1"), install.CLOUD["Google Gemini"])
        self.assertEqual(menu.call_args.args[2], names.index("Anthropic (Claude)"))   # the current one first
        with patch.object(install, "select", return_value=None), redirect_stdout(io.StringIO()):
            self.assertIsNone(install.pick_cloud(""))


class WindowSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.cfg, self.user, self.state = tmp / "config.json", tmp / "settings.json", tmp / "state.json"
        self.cfg.write_text(json.dumps({"base_url": "http://127.0.0.1:8080/v1", "server": "strata"}), encoding="utf-8")
        for name, path in (("CONFIG_FILE", self.cfg), ("USER_SETTINGS", self.user)):
            p = patch.object(appsettings, name, path)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(appsettings.config, "STATE_FILE", self.state)
        p.start()
        self.addCleanup(p.stop)

    def saved(self):
        return json.loads(self.cfg.read_text(encoding="utf-8"))

    def test_cloud_address_and_key_are_saved_with_its_kind(self):
        appsettings.apply({"base_url": "https://api.anthropic.com/v1", "api_key": "sk-ant-123"})
        cfg = self.saved()
        self.assertEqual((cfg["base_url"], cfg["api_key"], cfg["server"]),
                         ("https://api.anthropic.com/v1", "sk-ant-123", "anthropic"))

    def test_a_bare_ip_becomes_the_url_that_answers(self):
        with patch.object(servers, "find_base", return_value="http://192.168.0.10:1234/v1") as find:
            appsettings.apply({"base_url": "192.168.0.10"})
        cfg = self.saved()
        self.assertEqual(cfg["base_url"], "http://192.168.0.10:1234/v1")
        self.assertNotIn("server", cfg)          # an old kind is dropped: found again at the next run
        find.assert_called_once_with("192.168.0.10", "")


if __name__ == "__main__":
    unittest.main()
