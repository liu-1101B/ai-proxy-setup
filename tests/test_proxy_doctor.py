"""Offline regression tests for proxy diagnostic output safety."""
from pathlib import Path
import importlib.util
import unittest

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


if __name__ == "__main__":
    unittest.main()
