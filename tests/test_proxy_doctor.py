"""Offline regression tests for proxy diagnostic output safety."""
from pathlib import Path
import importlib.util
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "proxy_doctor.py"
spec = importlib.util.spec_from_file_location("proxy_doctor", SCRIPT)
proxy_doctor = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(proxy_doctor)


class ProxyDoctorRedactionTests(unittest.TestCase):
    def test_proxy_url_credentials_are_hidden(self):
        shown = proxy_doctor._safe_display_url(
            "http://alice:super-secret@127.0.0.1:7890"
        )
        self.assertEqual(shown, "http://<redacted>@127.0.0.1:7890")
        self.assertNotIn("alice", shown)
        self.assertNotIn("super-secret", shown)

    def test_query_values_and_fragment_are_hidden(self):
        shown = proxy_doctor._safe_display_url(
            "https://example.test/v1?token=topsecret&region=private#fragment"
        )
        self.assertNotIn("topsecret", shown)
        self.assertNotIn("private", shown)
        self.assertNotIn("fragment", shown)
        self.assertIn("token=", shown)
        self.assertIn("region=", shown)

    def test_no_proxy_keeps_loopback_but_masks_custom_hosts(self):
        shown = proxy_doctor._safe_no_proxy(
            "localhost,127.0.0.1,::1,internal.corp,secret.service"
        )
        self.assertEqual(
            shown,
            "localhost,127.0.0.1,::1,<custom-entry-1>,<custom-entry-2>",
        )
        self.assertNotIn("internal.corp", shown)
        self.assertNotIn("secret.service", shown)

    def test_environment_display_redacts_proxy_and_no_proxy_values(self):
        proxy = proxy_doctor._display_env_value(
            "HTTPS_PROXY", "http://user:password@proxy.local:7890"
        )
        bypass = proxy_doctor._display_env_value(
            "NO_PROXY", "localhost,private.internal"
        )
        self.assertNotIn("password", proxy)
        self.assertNotIn("private.internal", bypass)

    def test_error_text_redacts_credentials_and_query_values(self):
        shown = proxy_doctor._redact_text(
            "Request failed: https://name:password@example.test/path?key=secret"
        )
        self.assertNotIn("name", shown)
        self.assertNotIn("password", shown)
        self.assertNotIn("secret", shown)



    def test_dotenv_parser_only_returns_proxy_keys(self):
        values = proxy_doctor._parse_env_text(
            "export HTTPS_PROXY='http://user:password@127.0.0.1:7897'\n"
            "NO_PROXY=localhost,deepseek.com\n"
            "API_KEY=do-not-read-this\n"
            "NODE_USE_ENV_PROXY=1\n"
        )
        self.assertEqual(values["NO_PROXY"], "localhost,deepseek.com")
        self.assertNotIn("API_KEY", values)
        self.assertIn("HTTPS_PROXY", values)

    def test_proc_environ_parser_splits_nul_delimited_values(self):
        nul = bytes([0])
        raw = (
            b"PATH=/bin" + nul
            + b"HTTP_PROXY=http://127.0.0.1:7897" + nul
            + b"NO_PROXY=localhost,deepseek.com" + nul
            + b"SECRET=should-not-be-returned" + nul
        )
        values = proxy_doctor._parse_proc_env_bytes(raw)
        self.assertEqual(values["HTTP_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(values["NO_PROXY"], "localhost,deepseek.com")
        self.assertNotIn("SECRET", values)

    def test_global_leaf_node_is_reported_but_not_changed(self):
        payload = {
            "proxies": {
                "GLOBAL": {"name": "GLOBAL", "type": "Selector", "now": "broken-node"},
                "broken-node": {"name": "broken-node", "type": "Shadowsocks"},
            }
        }
        current, selected_type, message, is_leaf = proxy_doctor._summarize_global(payload)
        self.assertEqual(current, "broken-node")
        self.assertEqual(selected_type, "Shadowsocks")
        self.assertTrue(is_leaf)
        self.assertIn("不会自动切换", message)

    def test_global_urltest_group_is_not_misclassified_as_leaf(self):
        payload = {
            "proxies": {
                "GLOBAL": {"name": "GLOBAL", "type": "Selector", "now": "♻️ 自动选择"},
                "♻️ 自动选择": {"name": "♻️ 自动选择", "type": "URLTest"},
            }
        }
        current, selected_type, message, is_leaf = proxy_doctor._summarize_global(payload)
        self.assertEqual(current, "♻️ 自动选择")
        self.assertEqual(selected_type, "URLTest")
        self.assertFalse(is_leaf)

    def test_connections_use_current_metadata_host_and_chains(self):
        payload = {
            "connections": [
                {
                    "metadata": {"host": "hif-leim.deepseek.com", "destinationIP": "10.0.0.8"},
                    "chains": ["GLOBAL", "♻️ 自动选择", "node-a"],
                    "rule": "MATCH",
                }
            ]
        }
        rows = proxy_doctor._connection_rows(payload)
        self.assertEqual(rows[0][0], "hif-leim.deepseek.com")
        self.assertEqual(rows[0][1], "GLOBAL > ♻️ 自动选择 > node-a")
        self.assertEqual(rows[0][2], "MATCH")

    def test_controller_rejects_remote_plain_http_and_embedded_credentials(self):
        _, error = proxy_doctor._validate_controller_url("http://192.0.2.10:9799")
        self.assertIn("non-loopback HTTP", error)
        _, error = proxy_doctor._validate_controller_url("http://user:secret@127.0.0.1:9799")
        self.assertIn("credentials", error)

    def test_controller_accepts_loopback_http(self):
        base, error = proxy_doctor._validate_controller_url("http://127.0.0.1:9799/")
        self.assertEqual(base, "http://127.0.0.1:9799")
        self.assertEqual(error, "")

    @patch.object(proxy_doctor.subprocess, "run")
    def test_gnome_proxy_state_reads_manual_mode_and_ignore_hosts(self, mock_run):
        def fake_run(command, **kwargs):
            key = command[3]
            values = {
                "mode": "'manual'",
                "ignore-hosts": "['localhost', 'hif-leim.deepseek.com']",
                "host": "'127.0.0.1'",
                "port": "7897",
            }
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": values[key] + "\n", "stderr": ""},
            )()

        mock_run.side_effect = fake_run
        state = proxy_doctor._gnome_proxy_state()
        self.assertEqual(state["mode"], "'manual'")
        self.assertEqual(
            proxy_doctor._parse_gsettings_array(state["ignore-hosts"]),
            ["localhost", "hif-leim.deepseek.com"],
        )
        self.assertEqual(state["http-host"], "'127.0.0.1'")
        self.assertEqual(state["http-port"], "7897")
        self.assertNotIn("_error", state)

    def test_gsettings_list_parser(self):
        self.assertEqual(
            proxy_doctor._parse_gsettings_array("['localhost', 'hif-leim.deepseek.com']"),
            ["localhost", "hif-leim.deepseek.com"],
        )

if __name__ == "__main__":
    unittest.main()
